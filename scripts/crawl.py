#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LP看板 自动更新爬虫
- 抓取多个一级市场新闻源 -> 清洗 -> 按 8 赛道/5 类型启发式归类 -> 生成 data.js
- 防御式：每源独立容错，任一/全部源失败时保留上一次 data.js（不破坏看板）
- 说明：这是"标题级 + 启发式归类"，无 LLM 级 IR 启示/金额精修；type/amount/company 有误差
"""
import json, os, re, sys, time, datetime
import urllib.request, urllib.parse, urllib.error

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 仓库根
CONF_PATH = os.path.join(BASE, 'config', 'sources.json')
DATAJS_PATH = os.path.join(BASE, 'data.js')
TMPJS_PATH = os.path.join(BASE, 'data.js.tmp')

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'

# ---------- 类型判定（顺序敏感：先强信号） ----------
TYPE_RULES = [
    ("IPO",   re.compile(r'上市|IPO|递表|过会|招股|敲钟|挂牌|创业板|科创板|港交所|纳斯达克|路演')),
    ("融资",  re.compile(r'融资|获投|投资|募资|增资|Pre-A|A轮|B轮|C轮|D轮|Pre-IPO|亿(元|美元|港元)?融资|领投|跟投')),
    ("并购",  re.compile(r'收购|并购|重组|合并|控股|股权转让|要约|剥离')),
    ("政策",  re.compile(r'政策|监管|通知|规定|办法|意见|指引|条例|规划|发改委|工信部|证监会|央行')),
]

AMOUNT_RE = re.compile(r'([0-9]+(?:\.[0-9]+)?)\s*(亿元|亿|万美元|亿港元|亿欧元|万元|万港元|亿美元)')
CN_NUM_MAP = {'一':1,'二':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9,'十':10,'百':100,'千':1000,'万':10000,'亿':100000000}
CN_AMOUNT_RE = re.compile(r'([零一二三四五六七八九十百千万亿]+)\s*(亿元|亿美元|亿港元|亿元)')

def parse_cn_amount(s):
    """把中文数字金额粗略转成数值（仅亿元量级够用）"""
    m = CN_AMOUNT_RE.search(s)
    if not m: return None
    num, unit = m.group(1), m.group(2)
    if num == '亿': val = 1
    else:
        # 处理 "5.6亿"、"5亿"、"5000万" 等（万/亿混用常见）
        if '点' in num or '．' in num:
            try: val = float(num.replace('点','.'))
            except: return None
        elif '亿' in num:
            val = CN_NUM_MAP.get(num.rstrip('亿'), None)
            if val is None:
                # 多位如 "一百亿" -> 100
                try:
                    total=0; cur=0
                    for ch in num:
                        if ch in CN_NUM_MAP and CN_NUM_MAP[ch]<10000: cur = cur*10+CN_NUM_MAP[ch] if ch!='十' else (cur or 1)*10
                        elif ch=='亿': total=(total+cur)*CN_NUM_MAP['亿']; cur=0
                    val = total+cur
                except: return None
        else:
            try: val = int(num)
            except: return None
    if '亿' in unit:
        return float(val)
    if '万' in unit:
        return val/10000.0
    return float(val)

def extract_amount(text):
    m = AMOUNT_RE.search(text)
    if m:
        n = float(m.group(1)); unit = m.group(2)
        if '亿' in unit: return n, m.group(0)
        if '万' in unit: return n/10000.0, m.group(0)
        return n, m.group(0)
    cn = parse_cn_amount(text)
    if cn is not None:
        return cn, None
    return None, None

# ---------- 源抓取适配器 ----------
def fetch_jsonp(cfg, timeout=25):
    req = urllib.request.Request(cfg['url'], headers={'User-Agent': UA, 'Referer': 'https://www.eastmoney.com/'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode('utf-8', 'ignore')
    body = raw
    prefix = cfg.get('prefix', 'var ajaxResult=')
    if prefix and body.startswith(prefix):
        body = body[len(prefix):]
    body = body.strip().rstrip(';').strip()
    data = json.loads(body)
    items = data.get(cfg['list_key'], [])
    out = []
    for it in items:
        title = (it.get(cfg['title_key']) or '').strip()
        if not title: continue
        out.append({
            'title': title,
            'digest': (it.get(cfg.get('digest_key') or '') or '').strip(),
            'url': (it.get(cfg['url_key']) or '').strip(),
            'time': (it.get(cfg.get('time_key') or '') or '').strip(),
            'source': cfg['name'],
        })
    return out

def fetch_html(cfg, timeout=25):
    req = urllib.request.Request(cfg['url'], headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        html = r.read().decode('utf-8', 'ignore')
    # 投资界：抓取 <a href="https://news.pedaily.cn/<yyyymm>/<id>.shtml">标题</a>
    out = []
    seen = set()
    skip_titles = {'原文链接', '更多', '更多>>', '查看更多'}
    for m in re.finditer(r'<a[^>]*href="(https://news\.pedaily\.cn/[0-9]+/[0-9]+\.shtml)"[^>]*>([^<]{4,100})</a>', html):
        url, title = m.group(1), m.group(2).strip()
        if url in seen or title in skip_titles: continue
        seen.add(url); out.append({'title': title, 'digest': '', 'url': url, 'time': '', 'source': cfg['name']})
    return out

ADAPTERS = {'jsonp': fetch_jsonp, 'html': fetch_html}

def fetch_source(cfg):
    fn = ADAPTERS.get(cfg.get('type'))
    if not fn: return []
    return fn(cfg)

# ---------- 分类 ----------
def classify(tracks, companies, item):
    text = item['title'] + ' ' + item['digest']
    # 赛道
    track = None
    for t in tracks:
        if any(k in text for k in t['keywords']):
            track = t['name']; break
    if not track:
        return None  # 与 8 赛道无关，过滤（保持看板聚焦一级市场主题）
    # 类型
    etype = '动态'
    for ty, rx in TYPE_RULES:
        if rx.search(text):
            etype = ty; break
    # 金额
    amount, amount_raw = extract_amount(item['title'] + ' ' + item['digest'])
    # 公司
    company = None
    for c in companies:
        if c in text:
            company = c; break
    if company is None:
        # 常见"X公司完成..."模式
        mm = re.search(r'([一-龥A-Za-z0-9]{2,12})(?:完成|获|完成近|完成超)[\d.]*亿?(?:元|美元|港元|融资|Pre-A|A轮|B轮|C轮|D轮)', text)
        if mm: company = mm.group(1)
    return {
        'text': f"**{item['title']}**（[{item['source']}]({item['url']})）" + (f"：{item['digest'][:80]}" if item['digest'] else ''),
        'type': etype, 'amount': amount, 'amount_raw': amount_raw, 'company': company or '',
    }

# ---------- 组装 DATA ----------
def build_data(events, tracks):
    # events: list of (date, track_name, event_obj)
    by_date = {}
    for date, tname, ev in events:
        by_date.setdefault(date, {}).setdefault(tname, []).append(ev)
    dates = sorted(by_date.keys())
    # 只保留最近 4 天
    dates = dates[-4:]
    days = []
    for date in dates:
        day_tracks = []
        for t in tracks:
            evs = by_date.get(date, {}).get(t['name'], [])
            if not evs: continue
            day_tracks.append({
                'name': t['name'],
                'events': evs,
                'ir_insight': f"本日{t['name']}一级市场快讯 {len(evs)} 条（自动抓取·标题级）",
            })
        days.append({'date': date, 'tracks': day_tracks})
    all_events = [e for evs in by_date.values() for lst in evs.values() for e in lst]
    type_count = {}
    track_count = {}
    for e in all_events:
        type_count[e['type']] = type_count.get(e['type'], 0) + 1
    for _, tn, _ in events:
        track_count[tn] = track_count.get(tn, 0) + 1
    # company_dynamics
    company_dynamics = {}
    for date, tname, ev in events:
        if not ev['company']: continue
        company_dynamics.setdefault(ev['company'], []).append({
            'date': date, 'track': tname, 'type': ev['type'], 'amount': ev['amount'], 'text': ev['text']
        })
    return {
        'generated': datetime.date.today().isoformat(),
        'days': days,
        'stats': {
            'day_count': len(days),
            'event_count': len(all_events),
            'type_count': type_count,
            'track_count': track_count,
        },
        'company_dynamics': company_dynamics,
    }

def main():
    with open(CONF_PATH, 'r', encoding='utf-8') as f:
        conf = json.load(f)
    tracks, companies, sources = conf['tracks'], conf['companies'], conf['sources']

    raw_items = []
    any_success = False
    for src in sources:
        try:
            items = fetch_source(src)
            raw_items.extend(items)
            any_success = True
            print(f"[OK] {src['name']}: {len(items)} 条")
        except Exception as e:
            print(f"[FAIL] {src['name']}: {e}")
        time.sleep(0.5)

    if not any_success or not raw_items:
        print("所有源均失败或为空，保留上一次 data.js")
        sys.exit(0)

    # 去重（按 url）
    seen = set(); uniq = []
    for it in raw_items:
        k = it['url'] or it['title']
        if k in seen: continue
        seen.add(k); uniq.append(it)

    # 分类并过滤（仅保留命中 8 赛道的条目）
    events = []
    for it in uniq:
        ev = classify(tracks, companies, it)
        if ev is None: continue
        # 日期：优先源时间，否则今天
        date = it['time'][:10] if it['time'] and len(it['time']) >= 10 else datetime.date.today().isoformat()
        track = None
        text = it['title'] + ' ' + it['digest']
        for t in tracks:
            if any(k in text for k in t['keywords']):
                track = t['name']; break
        if track:
            events.append((date, track, ev))

    if not events:
        print("无命中 8 赛道的事件，保留上一次 data.js")
        sys.exit(0)

    DATA = build_data(events, tracks)

    # 写临时文件，成功后原子替换（防写一半损坏）
    with open(TMPJS_PATH, 'w', encoding='utf-8') as f:
        f.write('window.DATA = ' + json.dumps(DATA, ensure_ascii=False, indent=None) + ';\n')
    os.replace(TMPJS_PATH, DATAJS_PATH)

    print(f"完成：{DATA['stats']['event_count']} 条事件 / {len(DATA['days'])} 天 / {len(DATA['days'][0]['tracks'])} 赛道")
    print(f"类型分布: {DATA['stats']['type_count']}")

if __name__ == '__main__':
    main()
