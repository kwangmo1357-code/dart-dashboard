"""DART 재무제표 수집기

흐름: config.ini 읽기 → ① DART에서 재무제표 수집 → ② account_map.csv로 표준 계정 매핑
      → ③ 성장률·비율 계산 → data.js 저장 (dashboard.html이 이 파일을 읽음)

실행:
    python collect_dart.py                              # 실제 DART 수집 (dart_key.txt 또는 DART_API_KEY 필요)
    python collect_dart.py --sample sample_samsung.json # 인증키 없이 샘플로 화면 확인
"""
import configparser
import csv
import datetime
import io
import json
import math
import os
import re
import statistics
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
CACHE = HERE / "cache"
API = "https://opendart.fss.or.kr/api/"

# 이 상태코드가 오면 계속해도 소용없으므로 즉시 멈춘다
STOP_STATUS = {
    "010": "등록되지 않은 인증키입니다",
    "011": "사용할 수 없는 인증키입니다",
    "012": "접근할 수 없는 IP입니다",
    "020": "오늘 요청 한도를 넘었습니다",
    "100": "요청 값이 잘못되었습니다",
    "800": "DART 시스템 점검 중입니다",
    "900": "DART에서 정의되지 않은 오류가 났습니다",
    "901": "인증키 보유기간이 만료되었습니다",
}

# 표준 계정의 '재무제표' 칸 → 실제로 찾아볼 DART sj_div (손익은 IS와 CIS 둘 다)
SJ_SEARCH = {"IS": ("IS", "CIS"), "BS": ("BS",), "CF": ("CF",)}


# ───────────────────────── ① 수집 ─────────────────────────

def call_api(name, params, binary=False):
    """DART API 한 번 호출. binary=True면 압축파일 등 원본 바이트를 그대로 돌려준다."""
    url = API + name + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=30) as resp:
        body = resp.read()
    return body if binary else json.loads(body.decode("utf-8"))


def load_corp_codes(api_key):
    """종목코드(6자리) → (고유번호 8자리, 회사명) 사전. 목록은 cache에 한 번만 받아 둔다."""
    path = CACHE / "corpCode.xml"
    if not path.exists():
        body = call_api("corpCode.xml", {"crtfc_key": api_key}, binary=True)
        try:
            with zipfile.ZipFile(io.BytesIO(body)) as zf:
                path.write_bytes(zf.read(zf.namelist()[0]))
        except zipfile.BadZipFile:
            # 키가 틀리면 압축파일 대신 오류 메시지가 온다
            sys.exit("고유번호 목록을 받지 못했습니다: " + body[:300].decode("utf-8", "replace"))

    codes = {}
    for item in ET.parse(str(path)).getroot().iter("list"):
        stock = (item.findtext("stock_code") or "").strip()
        if stock:
            codes[stock] = (item.findtext("corp_code"), item.findtext("corp_name").strip())
    return codes


def fetch_statement(api_key, corp_code, year, fs_div, reprt_code="11011"):
    """한 회사·한 연도·한 보고서·연결/별도의 재무제표 전체 계정. 없으면 빈 목록."""
    suffix = "" if reprt_code == "11011" else "_" + reprt_code  # 사업보고서는 예전 파일 이름 유지
    path = CACHE / "{}_{}_{}{}.json".format(corp_code, year, fs_div, suffix)
    if path.exists():
        return json.loads(path.read_text("utf-8"))

    data = call_api("fnlttSinglAcntAll.json", {
        "crtfc_key": api_key,
        "corp_code": corp_code,
        "bsns_year": str(year),
        "reprt_code": reprt_code,  # 11011 사업 / 11012 반기 / 11013 1분기 / 11014 3분기
        "fs_div": fs_div,
    })
    time.sleep(0.3)  # DART 서버에 부담을 주지 않도록 잠깐 쉰다

    status = data.get("status")
    if status in STOP_STATUS:
        sys.exit("DART 오류 {}: {}".format(status, STOP_STATUS[status]))
    if status == "013":  # 조회된 데이터 없음 → 캐시하지 않음(나중에 공시될 수 있으므로)
        return []
    if status != "000":
        sys.exit("DART 응답 이상 {}: {}".format(status, data.get("message")))

    rows = data["list"]
    path.write_text(json.dumps(rows, ensure_ascii=False), "utf-8")
    return rows


# ───────────────────────── ② 매핑 ─────────────────────────

def load_account_map():
    """account_map.csv → [{표준계정, 재무제표, ids, names, 강조}, ...] (파일에 적힌 순서 유지)"""
    items = []
    with open(str(HERE / "account_map.csv"), encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            items.append({
                "name": row["표준계정"].strip(),
                "sj": row["재무제표"].strip(),
                "ids": [s.strip() for s in row["account_id"].split("|") if s.strip()],
                "names": [norm(s) for s in row["계정명"].split("|") if s.strip()],
                "bold": row["강조"].strip().upper() == "Y",
            })
    return items


def norm(text):
    """계정명 비교용: 띄어쓰기와 괄호 기호를 지운다. '영업이익 (손실)' → '영업이익손실'"""
    return re.sub(r"[\s()]", "", text or "")


def norm_id(account_id):
    """2018년 이전 공시는 'ifrs_Revenue', 이후는 'ifrs-full_Revenue' → 같은 코드로 본다."""
    return re.sub(r"^ifrs_", "ifrs-full_", account_id or "")


def to_number(text):
    """'1,234' → 1234, 빈칸이나 '-' → None"""
    text = (text or "").replace(",", "").strip()
    if text in ("", "-"):
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def map_accounts(rows, account_map):
    """DART 원본 행들에서 표준 계정 값을 뽑는다. 못 찾은 계정 이름 목록도 함께 돌려준다."""
    values, missing = {}, []
    for acc in account_map:
        found = None
        for sj in SJ_SEARCH[acc["sj"]]:
            pool = [r for r in rows if r.get("sj_div") == sj]
            # 1순위: account_id 일치
            found = next((r for r in pool if norm_id(r.get("account_id")) in acc["ids"]), None)
            # 2순위: 계정명 일치
            if found is None:
                found = next((r for r in pool if norm(r.get("account_nm")) in acc["names"]), None)
            if found is not None:
                break
        value = None
        if found:
            # 분기·반기보고서의 손익은 thstrm_amount가 3개월, thstrm_add_amount가 누적.
            # 누적으로 통일해 두고 기간 값은 나중에 빼서 만든다 (현금흐름은 원래 누적, 사업보고서는 add 없음)
            value = to_number(found.get("thstrm_add_amount")) if found.get("sj_div") in ("IS", "CIS") else None
            if value is None:
                value = to_number(found["thstrm_amount"])
        values[acc["name"]] = value
        if value is None:
            missing.append(acc["name"])
    return values, missing


def fill_derived(v):
    """공시에 없는 합계를 다른 계정으로 계산해서 채운다 (이미 있으면 그대로 둠)."""
    def sub(a, b):
        return None if v.get(a) is None or v.get(b) is None else v[a] - v[b]

    # 당기순이익 줄 없이 지배·비지배 귀속분만 공시한 경우
    if v.get("당기순이익") is None and v.get("지배주주순이익") is not None:
        v["당기순이익"] = v["지배주주순이익"] + (v.get("비지배주주순이익") or 0)
    if v.get("매출총이익") is None:
        v["매출총이익"] = sub("매출액", "매출원가")
    if v.get("판매비와관리비") is None:
        v["판매비와관리비"] = sub("매출총이익", "영업이익")
    if v.get("비유동자산") is None:
        v["비유동자산"] = sub("자산총계", "유동자산")
    if v.get("비유동부채") is None:
        v["비유동부채"] = sub("부채총계", "유동부채")
    # 잉여현금흐름 = 영업활동현금흐름 − 유형자산 취득액(부호와 상관없이 지출액으로 봄)
    if v.get("영업활동현금흐름") is not None and v.get("유형자산취득") is not None:
        v["잉여현금흐름"] = v["영업활동현금흐름"] - abs(v["유형자산취득"])
    else:
        v["잉여현금흐름"] = None
    return v


# ───────────────────────── ③ 계산 ─────────────────────────

def growth(cur, prev):
    """전년 대비 성장률(%). 분모에 절댓값을 써서 적자→흑자 때 부호가 뒤집히지 않게 한다."""
    if cur is None or prev is None or prev == 0:
        return None
    return (cur - prev) / abs(prev) * 100


def ratio(a, b, scale=100):
    if a is None or b is None or b == 0:
        return None
    return a / b * scale


def average(cur, prev):
    """(기초 + 기말) ÷ 2. 기초(전년 기말)가 없으면 기말 값만 쓴다."""
    if cur is None:
        return None
    return cur if prev is None else (cur + prev) / 2


# 재무비율 정의: (구분, 이름, 단위, 좋은 방향, 계산 함수)
#   계산 함수는 (당기 값 사전, 전기 값 사전)을 받는다.
def _roe(c, p):
    if c.get("지배주주순이익") is not None and c.get("지배주주지분") is not None:
        return ratio(c["지배주주순이익"], average(c["지배주주지분"], p.get("지배주주지분")))
    return ratio(c.get("당기순이익"), average(c.get("자본총계"), p.get("자본총계")))


RATIOS = [
    ("수익성", "매출총이익률", "%", "high", lambda c, p: ratio(c.get("매출총이익"), c.get("매출액"))),
    ("수익성", "영업이익률", "%", "high", lambda c, p: ratio(c.get("영업이익"), c.get("매출액"))),
    ("수익성", "순이익률", "%", "high", lambda c, p: ratio(c.get("당기순이익"), c.get("매출액"))),
    ("수익성", "ROE", "%", "high", _roe),
    ("수익성", "ROA", "%", "high",
     lambda c, p: ratio(c.get("당기순이익"), average(c.get("자산총계"), p.get("자산총계")))),
    ("안정성", "부채비율", "%", "low", lambda c, p: ratio(c.get("부채총계"), c.get("자본총계"))),
    ("안정성", "유동비율", "%", "high", lambda c, p: ratio(c.get("유동자산"), c.get("유동부채"))),
    # 본문 재무제표에는 '이자비용' 줄이 없고, 금융비용에는 외환손실 등이 섞여 있어 실제 이자보상배율보다 낮게 나온다
    ("안정성", "금융비용보상배율", "배", "high", lambda c, p: ratio(c.get("영업이익"), c.get("금융비용"), 1)),
    ("활동성", "총자산회전율", "회", "high",
     lambda c, p: ratio(c.get("매출액"), average(c.get("자산총계"), p.get("자산총계")), 1)),
]


# 분기·반기에서는 1년치 이익이 필요한 비율 → 최근 4분기(2반기) 합계(TTM)로 계산
TTM_RATIOS = {"ROE", "ROA", "총자산회전율"}


def build_view(labels, rows, fs, layout_names, flow_names, step):
    """기간별 값 목록 → 표·차트용 묶음. 연간·반기·분기가 모두 이 함수 하나를 쓴다.
    step = 1년에 해당하는 칸 수 (연간 1, 반기 2, 분기 4)
      - 성장률: step칸 앞(= 전년 같은 기간)과 비교
      - TTM_RATIOS: 손익·현금흐름(flow)은 최근 step칸 합계, 평균 잔액의 기초는 step칸 앞의 잔액"""
    empty = {}
    rows = [r or empty for r in rows]
    accounts = {n: [r.get(n) for r in rows] for n in layout_names}

    def ago(vals, i):
        return vals[i - step] if i >= step else None
    yoy = {n: [growth(v[i], ago(v, i)) for i in range(len(v))] for n, v in accounts.items()}

    def ttm(i):
        if step == 1:
            return rows[i]
        if i < step - 1:
            return empty
        window = rows[i - step + 1:i + 1]
        out = dict(rows[i])  # 잔액(재무상태표)은 그대로, 손익·현금흐름만 합계로 교체
        for n in flow_names:
            vals = [w.get(n) for w in window]
            out[n] = None if any(v is None for v in vals) else sum(vals)
        return out

    ratios = []
    for group, rname, unit, better, func in RATIOS:
        vals = []
        for i in range(len(rows)):
            prev = rows[i - step] if i >= step else empty
            cur = ttm(i) if rname in TTM_RATIOS else rows[i]
            vals.append(func(cur, prev))
        ratios.append({"group": group, "name": rname, "unit": unit, "better": better,
                       "ttm": step > 1 and rname in TTM_RATIOS,
                       "values": [None if v is None else round(v, 2) for v in vals]})

    return {"labels": labels, "fs": fs, "accounts": accounts,
            "yoy": {n: [None if v is None else round(v, 2) for v in vals] for n, vals in yoy.items()},
            "ratios": ratios}


def build_company(stock_code, name, fs_div, by_year, layout_names, basis=None, flow_names=()):
    """연도별 표준 계정 값 → 대시보드용 묶음(계정 시계열, 성장률, 비율).
    basis: {연도: 'CFS'/'OFS'} — 중간에 연결로 바뀐 회사는 해마다 기준이 다르다."""
    years = list(range(min(by_year), max(by_year) + 1))  # 빠진 해도 빈칸으로 자리를 둔다
    fs_years = [(basis or {}).get(y, fs_div if y in by_year else None) for y in years]
    view = build_view(["FY{}".format(y) for y in years], [by_year.get(y) for y in years],
                      fs_years, layout_names, flow_names, 1)
    return {"stock_code": stock_code, "name": name, "fs_div": fs_div, "fs_years": fs_years, "years": years,
            "accounts": view["accounts"], "yoy": view["yoy"], "ratios": view["ratios"],
            "views": {"annual": view}}


# 분기·반기 보고서: 이름 → (보고서 코드, 누적 개월 수)
SUB_REPORTS = [("Q1", "11013"), ("H1", "11012"), ("Q3", "11014"), ("FY", "11011")]


def collect_cumulative(api_key, corp_code, first, last, prefer, account_map):
    """연도별 1분기·반기·3분기·연간 보고서의 누적 값(손익·현금흐름)과 잔액(재무상태표).
    결과: {(연도, 'Q1'|'H1'|'Q3'|'FY'): (값 사전, 'CFS'|'OFS')}"""
    cum = {}
    for year in range(first, last + 1):
        for key, code in SUB_REPORTS:
            rows, fs = fetch_statement(api_key, corp_code, year, prefer, code), prefer
            if not rows and prefer == "CFS":
                rows, fs = fetch_statement(api_key, corp_code, year, "OFS", code), "OFS"
            if rows:
                cum[(year, key)] = (fill_derived(map_accounts(rows, account_map)[0]), fs)
    return cum


def make_sub_views(cum, layout_names, flow_names):
    """누적 값 → 반기·분기 기간 값. 손익·현금흐름은 '이번 누적 − 직전 누적', 재무상태표는 그 시점 잔액.
    두 보고서의 기준(연결/별도)이 다르면 빼기가 의미 없으므로 그 칸은 비운다."""
    def period(year, key, prev_key):
        if (year, key) not in cum:
            return None, None
        cur, fs = cum[(year, key)]
        out = dict(cur)
        if prev_key:
            prev = cum.get((year, prev_key))
            same = prev is not None and prev[1] == fs
            for n in flow_names:
                out[n] = (cur.get(n) - prev[0].get(n)
                          if same and cur.get(n) is not None and prev[0].get(n) is not None else None)
        return out, fs

    years = sorted({y for y, _ in cum})
    specs = {
        "half": (2, [("1H", "H1", None), ("2H", "FY", "H1")]),
        "quarter": (4, [("1Q", "Q1", None), ("2Q", "H1", "Q1"), ("3Q", "Q3", "H1"), ("4Q", "FY", "Q3")]),
    }
    views = {}
    for unit, (step, parts) in specs.items():
        labels, rows, fs_list = [], [], []
        for year in years:
            for label, key, prev_key in parts:
                row, fs = period(year, key, prev_key)
                labels.append("{}.{}".format(year, label))
                rows.append(row)
                fs_list.append(fs)
        # 앞뒤로 아직 공시가 없는 칸은 잘라낸다 (중간 빈칸은 자리를 남겨 둠)
        filled = [i for i, r in enumerate(rows) if r is not None]
        if not filled:
            continue
        a, b = filled[0], filled[-1] + 1
        views[unit] = build_view(labels[a:b], rows[a:b], fs_list[a:b], layout_names, flow_names, step)
    return views


def make_layout(account_map):
    """화면에 그릴 표의 행 순서. account_map 순서 + 계산으로 만든 잉여현금흐름."""
    layout = {"IS": [], "BS": [], "CF": []}
    for acc in account_map:
        layout[acc["sj"]].append({"name": acc["name"], "bold": acc["bold"]})
    layout["CF"].append({"name": "잉여현금흐름", "bold": True})
    return layout


def write_data_js(companies, layout, sample):
    payload = {
        "generated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "sample": sample,
        "unit": "원",
        "layout": layout,
        "companies": companies,
    }
    text = "window.DASH_DATA = " + json.dumps(payload, ensure_ascii=False) + ";\n"
    (HERE / "data.js").write_text(text, "utf-8")


# ───────────────────────── 회사별 연도 수집 ─────────────────────────

def collect_years(api_key, corp_code, corp_name, first, last, prefer, account_map, report=None):
    """한 회사의 first~last년 표준 계정 값과 연도별 기준(연결/별도).
    report 목록을 주면 못 찾은 계정을 기록한다."""
    by_year, basis = {}, {}
    for year in range(first, last + 1):
        rows, basis[year] = fetch_statement(api_key, corp_code, year, prefer), prefer
        if not rows and prefer == "CFS":  # 연결이 없는 해(종속회사가 없던 해)는 별도로
            rows, basis[year] = fetch_statement(api_key, corp_code, year, "OFS"), "OFS"
        if not rows:
            del basis[year]
            if report is not None:
                print("   {}년: 데이터 없음".format(year))
            continue
        values, missing = map_accounts(rows, account_map)
        by_year[year] = fill_derived(values)
        if report is not None:
            for m in missing:
                if by_year[year].get(m) is not None:  # 계산으로 채운 것은 보고하지 않음
                    continue
                if "지배" in m and basis[year] == "OFS":  # 별도재무제표엔 지배·비지배 구분이 원래 없음
                    continue
                if m == "비지배주주순이익":  # 비지배지분이 없는 회사는 줄 자체가 없음
                    continue
                report.append({"회사": corp_name, "연도": year, "표준계정": m})
    return by_year, basis


# ───────────────────────── 업종 중앙값 ─────────────────────────

def load_industry_index(api_key, corp_codes):
    """전체 상장사의 업종코드(KSIC) 목록. 처음 한 번만 기업개황 API로 모으고 cache에 저장한다.
    중간에 멈춰도 다음 실행 때 남은 회사부터 이어서 받는다."""
    path = CACHE / "industry_index.json"
    index = json.loads(path.read_text("utf-8")) if path.exists() else {}
    todo = [s for s in corp_codes if s not in index]
    if todo:
        print("업종코드 수집: {}개 회사 (처음 한 번만, 약 {}분)".format(len(todo), len(todo) // 150 + 1))
    for i, stock in enumerate(todo, 1):
        corp_code, name = corp_codes[stock]
        data = call_api("company.json", {"crtfc_key": api_key, "corp_code": corp_code})
        time.sleep(0.15)
        status = data.get("status")
        if status in STOP_STATUS:
            path.write_text(json.dumps(index, ensure_ascii=False), "utf-8")  # 받은 데까지 저장
            sys.exit("DART 오류 {}: {} (업종코드 {}개까지 저장됨, 다시 실행하면 이어서 받음)".format(
                status, STOP_STATUS[status], len(index)))
        index[stock] = {"corp_code": corp_code, "name": name,
                        "induty": data.get("induty_code", "") if status == "000" else "",
                        "cls": data.get("corp_cls", "") if status == "000" else ""}
        if i % 200 == 0 or i == len(todo):
            path.write_text(json.dumps(index, ensure_ascii=False), "utf-8")
            print("   {}/{}".format(i, len(todo)))
    return index


def fetch_assets(api_key, corp_list, year):
    """여러 회사의 자산총계를 다중회사 주요계정 API로 한 번에(100개씩) 받는다. 연결 우선."""
    path = CACHE / "assets_{}.json".format(year)
    assets = json.loads(path.read_text("utf-8")) if path.exists() else {}
    todo = [c for c in corp_list if c not in assets]
    for start in range(0, len(todo), 100):
        chunk = todo[start:start + 100]
        data = call_api("fnlttMultiAcnt.json", {"crtfc_key": api_key, "corp_code": ",".join(chunk),
                                                "bsns_year": str(year), "reprt_code": "11011"})
        time.sleep(0.3)
        found = {}
        for r in data.get("list", []):
            if r.get("account_nm") == "자산총계" and r.get("sj_div") == "BS":
                if r["corp_code"] not in found or r.get("fs_div") == "CFS":
                    found[r["corp_code"]] = to_number(r.get("thstrm_amount"))
        for c in chunk:
            assets[c] = found.get(c)
        path.write_text(json.dumps(assets, ensure_ascii=False), "utf-8")
    return assets


def parse_manual_peers(spec, stock, index):
    """config.ini의 직접 지정 문자열 → 종목코드 목록.
    '489790, ksic:26429, -143160' = 489790 추가, 업종 26429* 전부 추가, 143160 제외"""
    include, exclude = [], {stock}
    for token in (t.strip() for t in spec.split(",")):
        if not token:
            continue
        if token.startswith("-"):
            exclude.add(token[1:].strip())
        elif token.lower().startswith("ksic:"):
            prefix = token[5:].strip()
            include += [s for s, v in index.items()
                        if v["cls"] in ("Y", "K") and v["induty"].startswith(prefix)]
        elif token in index:
            include.append(token)
        else:
            print("   [직접 지정] 종목코드를 찾을 수 없어 건너뜀:", token)
    seen = set()  # 순서 유지 + 중복 제거
    return [s for s in include if s not in exclude and not (s in seen or seen.add(s))]


def find_peers(api_key, stock, own_assets, year, index, min_peers, max_peers, manual_spec=None):
    """비교 그룹 고르기.
    - 직접 지정이 있으면 그 목록을 쓴다.
    - 없으면 업종코드가 같은 유가증권·코스닥 상장사. 너무 적으면 코드를 한 자리씩 짧게 잘라 넓힌다.
    어느 쪽이든 너무 많으면 자산총계가 우리 회사와 비슷한 순으로 max_peers개만 고른다."""
    if manual_spec:
        prefix, cands = "직접 지정", parse_manual_peers(manual_spec, stock, index)
    else:
        code = index.get(stock, {}).get("induty", "")
        if not code:
            return "", []
        prefix, cands = code, []
        for n in range(len(code), 1, -1):
            prefix = code[:n]
            cands = [s for s, v in index.items()
                     if s != stock and v["cls"] in ("Y", "K") and v["induty"].startswith(prefix)]
            if len(cands) >= min_peers:
                break
    if len(cands) > max_peers:
        assets = fetch_assets(api_key, [index[s]["corp_code"] for s in cands], year)

        def distance(s):  # 규모 차이: 자산 비율의 로그값 (2배 크든 절반이든 같은 거리)
            a = assets.get(index[s]["corp_code"])
            if not a or not own_assets or a <= 0 or own_assets <= 0:
                return float("inf")
            return abs(math.log(a / own_assets))
        cands = sorted(cands, key=distance)[:max_peers]
    return prefix, cands


def industry_median(api_key, stock, by_year, index, corp_codes, min_peers, max_peers,
                    prefer, account_map, names, manual_spec=None):
    """우리 회사 최근 연도 기준, 비교 그룹 각 회사의 재무비율 → 비율별 중앙값."""
    year = max(by_year)
    prefix, peers = find_peers(api_key, stock, by_year[year].get("자산총계"), year,
                               index, min_peers, max_peers, manual_spec)
    if not peers:
        print("   비교 그룹이 비어 업종 중앙값을 건너뜁니다")
        return None
    print("   비교 그룹: {} {}개사".format(
        prefix if manual_spec else "업종코드 {}*".format(prefix), len(peers)))

    values = {}   # 비율 이름 → 비교 회사들의 값 목록
    used_peers = []
    for s in peers:
        corp_code, name = corp_codes[s]
        # 평균 잔액을 쓰는 비율(ROE 등) 때문에 전년도도 함께 받는다
        peer_years, _ = collect_years(api_key, corp_code, name, year - 1, year, prefer, account_map)
        if year not in peer_years:
            continue
        peer = build_company(s, name, "", peer_years, names)
        used_peers.append({"stock_code": s, "name": name})
        for r in peer["ratios"]:
            if r["values"][-1] is not None:
                values.setdefault(r["name"], []).append(r["values"][-1])

    return {
        "code": prefix,
        "manual": bool(manual_spec),
        "full_code": index[stock]["induty"],
        "year": year,
        "peers": used_peers,
        "median": {k: round(statistics.median(v), 2) for k, v in values.items()},
        "count": {k: len(v) for k, v in values.items()},
    }


# ───────────────────────── 지분·공시 (비재무) ─────────────────────────

def fetch_list(api_key, name, params, cache_name=None):
    """목록형 API 한 번 호출. cache_name을 주면 결과를 저장해 두고 재사용한다(연도별 고정 자료용).
    5% 보고·소유보고·공시목록처럼 수시로 바뀌는 자료는 cache_name 없이 매번 새로 받는다."""
    path = CACHE / cache_name if cache_name else None
    if path and path.exists():
        return json.loads(path.read_text("utf-8"))
    data = call_api(name, dict(params, crtfc_key=api_key))
    time.sleep(0.3)
    status = data.get("status")
    if status in STOP_STATUS:
        sys.exit("DART 오류 {}: {}".format(status, STOP_STATUS[status]))
    rows = data.get("list", []) if status == "000" else []
    if path and rows:
        path.write_text(json.dumps(rows, ensure_ascii=False), "utf-8")
    return rows


def to_float(text):
    """'52.80' → 52.8, '-1.78' → -1.78, 빈칸·'-' → None"""
    text = (text or "").replace(",", "").strip()
    try:
        return float(text) if text not in ("", "-") else None
    except ValueError:
        return None


def collect_ownership(api_key, corp_code, first, last):
    """최대주주 현황(연도별), 5% 대량보유 보고, 임원·주요주주 소유 보고, 최근 공시 목록."""
    # ① 최대주주 현황: 사업보고서에 실린 연도별 자료 → 캐시
    trend, latest = [], None
    for year in range(first, last + 1):
        rows = fetch_list(api_key, "hyslrSttus.json",
                          {"corp_code": corp_code, "bsns_year": str(year), "reprt_code": "11011"},
                          "{}_{}_hyslr.json".format(corp_code, year))
        common = [r for r in rows if "보통" in r.get("stock_knd", "")] or rows   # 보통주 기준
        if not common:
            continue
        total = next((r for r in common if r.get("nm", "").strip() in ("계", "합계")), None)
        people = [r for r in common if r is not total]
        if total is not None:
            rate = to_float(total.get("trmend_posesn_stock_qota_rt"))
        else:  # 합계 줄이 없으면 주주별 기말 지분율을 더한다
            rate = round(sum(to_float(r.get("trmend_posesn_stock_qota_rt")) or 0 for r in people), 2)
        trend.append({"year": year, "rate": rate})
        latest = {"year": year, "holders": sorted([{
            "name": r.get("nm", "").strip(),
            "relate": r.get("relate", "").strip(),
            "begin_rate": to_float(r.get("bsis_posesn_stock_qota_rt")),
            "end_rate": to_float(r.get("trmend_posesn_stock_qota_rt")),
            "end_qty": to_number(r.get("trmend_posesn_stock_co")),
        } for r in people], key=lambda h: -(h["end_rate"] or 0))}

    # ② 5% 대량보유 보고 (특별관계자 합산 비율)
    major = [{
        "date": r.get("rcept_dt"), "reporter": r.get("repror", "").strip(), "type": r.get("report_tp"),
        "qty": to_number(r.get("stkqy")), "qty_chg": to_number(r.get("stkqy_irds")),
        "rate": to_float(r.get("stkrt")), "rate_chg": to_float(r.get("stkrt_irds")),
        "reason": (r.get("report_resn") or "").strip(), "rcept_no": r.get("rcept_no"),
    } for r in fetch_list(api_key, "majorstock.json", {"corp_code": corp_code})]

    # ③ 임원·주요주주 소유 보고 (본인 지분만)
    insider = [{
        "date": r.get("rcept_dt"), "reporter": r.get("repror", "").strip(),
        "registered": r.get("isu_exctv_rgist_at"), "position": r.get("isu_exctv_ofcps"),
        "major": r.get("isu_main_shrholdr"),
        "qty": to_number(r.get("sp_stock_lmp_cnt")), "qty_chg": to_number(r.get("sp_stock_lmp_irds_cnt")),
        "rate": to_float(r.get("sp_stock_lmp_rate")), "rate_chg": to_float(r.get("sp_stock_lmp_irds_rate")),
        "rcept_no": r.get("rcept_no"),
    } for r in fetch_list(api_key, "elestock.json", {"corp_code": corp_code})]

    # ④ 최근 1년 공시 20건 (최신순)
    since = (datetime.date.today() - datetime.timedelta(days=365)).strftime("%Y%m%d")
    filings = [{
        "date": "{}-{}-{}".format(r["rcept_dt"][:4], r["rcept_dt"][4:6], r["rcept_dt"][6:]),
        "title": re.sub(r"\s+", " ", r.get("report_nm", "")).strip(),
        "filer": r.get("flr_nm", "").strip(), "rcept_no": r.get("rcept_no"),
    } for r in fetch_list(api_key, "list.json",
                          {"corp_code": corp_code, "bgn_de": since, "page_count": "20"})]

    return {"holder_trend": trend, "holders": latest,
            "major": sorted(major, key=lambda m: m["date"] or ""),
            "insider": sorted(insider, key=lambda m: m["date"] or ""),
            "filings": filings}


# ───────────────────────── 실행 ─────────────────────────

def run_sample(sample_path, account_map):
    src = json.loads(Path(sample_path).read_text("utf-8"))
    by_year = {int(y): fill_derived(dict(v)) for y, v in src["years"].items()}
    layout = make_layout(account_map)
    names = [r["name"] for rows in layout.values() for r in rows]
    company = build_company(src["stock_code"], src["name"], src["fs_div"], by_year, names)
    write_data_js([company], layout, sample=True)
    print("샘플로 data.js를 만들었습니다:", src["name"], company["years"][0], "~", company["years"][-1])


def load_api_key():
    """인증키 찾는 순서: ① 환경변수 DART_API_KEY (GitHub Actions가 Secrets에서 넣어 줌)
    ② dart_key.txt (내 컴퓨터, .gitignore로 업로드 안 됨). 공개 저장소에 올라가는 config.ini에는 두지 않는다."""
    key = os.environ.get("DART_API_KEY", "").strip()
    path = HERE / "dart_key.txt"
    if not key and path.exists():
        key = path.read_text("utf-8").strip()
    return key


def run_dart(account_map):
    cfg = configparser.ConfigParser()
    cfg.read(str(HERE / "config.ini"), encoding="utf-8")
    api_key = load_api_key()
    if not api_key:
        sys.exit("인증키가 없습니다. opendart.fss.or.kr에서 발급받은 키를 dart_key.txt에 넣어 주세요 "
                 "(GitHub Actions에서는 Secrets의 DART_API_KEY).")
    stocks = [s.strip() for s in cfg.get("collect", "companies").split(",") if s.strip()]
    first, last = cfg.getint("collect", "first_year"), cfg.getint("collect", "last_year")
    prefer = cfg.get("collect", "fs_div", fallback="CFS").strip().upper()

    peer_mode = cfg.get("peers", "mode", fallback="off").strip().lower()
    min_peers = cfg.getint("peers", "min_peers", fallback=5)
    max_peers = cfg.getint("peers", "max_peers", fallback=15)
    # [peers]에서 키가 종목코드(숫자 6자리)인 줄 = 회사별 직접 지정
    manual = {k: v for k, v in cfg.items("peers")} if cfg.has_section("peers") else {}
    manual = {k: v for k, v in manual.items() if re.fullmatch(r"\d{6}", k)}

    CACHE.mkdir(exist_ok=True)
    corp_codes = load_corp_codes(api_key)
    layout = make_layout(account_map)
    names = [r["name"] for rows in layout.values() for r in rows]
    flows = [r["name"] for sj in ("IS", "CF") for r in layout[sj]]  # 기간 값(빼기·합계 대상)
    companies, report = [], []
    index =load_industry_index(api_key, corp_codes) if peer_mode == "auto" else None

    for stock in stocks:
        if stock not in corp_codes:
            print("[건너뜀] 종목코드를 찾을 수 없습니다:", stock)
            continue
        corp_code, corp_name = corp_codes[stock]
        print("수집 중:", corp_name, stock)
        by_year, basis = collect_years(api_key, corp_code, corp_name, first, last, prefer,
                                       account_map, report)
        if not by_year:
            continue
        company = build_company(stock, corp_name, basis[max(basis)], by_year, names, basis, flows)
        # 분기·반기: DART는 2016년부터 분기보고서를 제공. 올해 나온 분기까지 포함한다
        cum = collect_cumulative(api_key, corp_code, max(first, 2016), datetime.date.today().year,
                                 prefer, account_map)
        company["views"].update(make_sub_views(cum, names, flows))
        if index is not None:
            company["industry"] = industry_median(api_key, stock, by_year, index, corp_codes,
                                                  min_peers, max_peers, prefer, account_map, names,
                                                  manual.get(stock))
        company["ownership"] = collect_ownership(api_key, corp_code, first, last)
        companies.append(company)

    if not companies:
        sys.exit("수집된 회사가 없습니다.")
    write_data_js(companies, layout, sample=False)
    with open(str(HERE / "미매핑_보고서.csv"), "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["회사", "연도", "표준계정"])
        writer.writeheader()
        writer.writerows(report)
    print("완료: data.js ({}개 회사), 미매핑 {}건 → 미매핑_보고서.csv".format(len(companies), len(report)))


def main():
    account_map = load_account_map()
    if len(sys.argv) >= 3 and sys.argv[1] == "--sample":
        run_sample(sys.argv[2], account_map)
    else:
        run_dart(account_map)


if __name__ == "__main__":
    main()
