"""
국토교통부 아파트 매매 실거래가 — 수집 · 정리 · 집계  (v7)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
이 스크립트가 하는 일
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
공공데이터 API에서 서울 일부 구의 아파트 매매 신고자료를 받아,
생활권·단지·월 단위로 집계한다. 산출물은 CSV 여섯 개다.

    00_raw_api.csv   API 응답 원본 (컬럼명 그대로)
    01_deals.csv     거래 한 건 = 한 행. 파생 컬럼 포함
    02_complex.csv   단지별 집계
    03_region.csv    구별 집계
    04_trend.csv     월별 거래건수
    05_floor.csv     층 보정 후 남는 가격 분산

단계를 셋으로 나눠 각 단계마다 CSV를 떨군다.

    collect()  →  tidy()  →  summarize_*()
      API         정리        집계

이렇게 하면 집계 방식을 바꿀 때마다 API를 다시 부르지 않아도 된다.
개발계정은 일일 호출 한도가 있어서 이게 중요하다.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
집계하지 않는 것
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
이 자료에는 거래 사실만 들어 있다. 왜 그렇게 거래됐는지는 없다.
따라서 관측값의 원인을 코드나 주석에서 단정하지 않는다.
정비사업 단계·규제 해당 여부 같은 외부 정보로 거래를 분류하는 기능은
v7에서 걷어냈다. 그런 분류는 이 자료가 아니라 별도 조사의 몫이다.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
실행
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    pip install requests pandas numpy
    export MOLIT_KEY="발급받은_인증키"      # Encoding/Decoding 아무거나
    python molit_screening.py
"""

import os
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime
from urllib.parse import quote, unquote, urlencode

import numpy as np
import pandas as pd
import requests

# ═══════════════════════════════════════════════════════════════
# 0. 설정
# ═══════════════════════════════════════════════════════════════
# [인증키] Encoding 키든 Decoding 키든 아래 build_url()이 알아서 처리한다.
# data.go.kr은 같은 키를 두 형태로 준다.
#   Decoding 키 :  ...Alv/dVr1...azQ==       (원본. + / = 가 날것)
#   Encoding 키 :  ...Alv%2FdVr1...azQ%3D%3D (위를 URL 인코딩한 것)
# requests에 params= 딕셔너리로 넘기면 requests가 한 번 더 인코딩하므로,
# Encoding 키를 그렇게 넘기면 %2F가 %252F가 되는 '이중 인코딩'이 되어
# 서버가 키를 못 알아보고 403을 낸다. 그래서 URL을 직접 만든다.
SERVICE_KEY = os.environ.get("MOLIT_KEY", "여기에_인증키")

# [엔드포인트가 둘인 이유] data.go.kr에는 비슷한 API가 나란히 있고,
# 첨부된 기술문서(hwp)와 마이페이지의 End Point가 서로 다를 수 있다.
#   RTMSDataSvcAptTradeDev  ← 이 계정이 승인받은 것 (마이페이지 표시)
#   RTMSDataSvcAptTrade     ← 기술문서에 적힌 것
# 문서보다 '내 계정 마이페이지의 End Point'가 언제나 우선이다.
# 어느 쪽이 살아 있는지 아래 diagnose()가 직접 찔러보고 정한다.
ENDPOINTS = [
    "https://apis.data.go.kr/1613000/RTMSDataSvcAptTradeDev/getRTMSDataSvcAptTradeDev",
    "https://apis.data.go.kr/1613000/RTMSDataSvcAptTrade/getRTMSDataSvcAptTrade",
]
ENDPOINT = ENDPOINTS[0]   # diagnose()가 실행되면 살아있는 쪽으로 교체된다

# 조회할 구. 키는 법정동코드 5자리(LAWD_CD).
# 서울 전체가 아니라 다섯 개만 둔 것은 개발계정 일일 호출 한도 때문이다.
# 아래 [개발계정 트래픽] 주석 참고. 코드는 구 수에 무관하니 원하는 대로 바꾸면 된다.
REGIONS = {
    "11680": "강남구",
    "11650": "서초구",
    "11710": "송파구",
    "11470": "양천구",     # 목동
    "11560": "영등포구",   # 여의도
}

YEARS_BACK = 3     # 몇 년치를 받을 것인가
SLEEP_SEC = 0.12   # 문서상 30 TPS 허용 → 초당 8회는 넉넉히 안전
MIN_DEALS = 10     # 단지별 집계에서 이보다 적은 단지는 뺀다

# [개발계정 트래픽] 에러 22가 '일일 활용건수 초과'다. 개발계정은 보통
# 일 1,000건 한도. 5개 구 × 36개월 = 180회(+페이지)이므로 하루에
# 두세 번 돌리는 정도는 괜찮지만, 구를 더 늘리면 걸릴 수 있다.

# 기술문서 Ⅱ장의 에러코드표. 원인을 즉시 알려주려고 그대로 옮겼다.
ERROR_MSG = {
    "01": "제공기관 서비스 오류(Application Error) — 잠시 후 재시도",
    "02": "제공기관 DB 오류 — 잠시 후 재시도",
    "03": "데이터 없음 (해당 월 거래가 없을 수 있음 — 정상일 수 있다)",
    "04": "HTTP Error — 잠시 후 재시도",
    "05": "서비스 타임아웃 — 잠시 후 재시도",
    "10": "serviceKey 파라미터 누락",
    "11": "필수 파라미터 누락 (LAWD_CD / DEAL_YMD 확인)",
    "12": "서비스 URL이 잘못되었거나 폐기된 서비스",
    "20": "★ 활용승인이 안 된 상태입니다. data.go.kr 마이페이지에서 승인 확인",
    "22": "★ 일일 트래픽 초과. 개발계정 한도를 넘었습니다 (내일 재시도)",
    "30": "★ 등록되지 않은 서비스키. Decoding 키를 쓰고 있는지 확인",
    "31": "서비스키 기간 만료 — 연장신청 필요",
    "32": "등록되지 않은 도메인/IP",
}


# ═══════════════════════════════════════════════════════════════
# 1. 수집 — API에서 받아오기
# ═══════════════════════════════════════════════════════════════
def month_list(years_back: int) -> list[str]:
    """조회할 '202508' 형태의 월 목록.

    API가 기간 조회를 지원하지 않고 한 번에 한 달치만 준다(DEAL_YMD 6자리).
    그래서 호출할 월을 우리가 전부 나열해야 한다.
    """
    today = date.today()
    out, y, m = [], today.year, today.month
    for _ in range(years_back * 12):
        m -= 1                     # 이번 달은 집계 중이므로 제외
        if m == 0:
            y, m = y - 1, 12
        out.append(f"{y}{m:02d}")
    return sorted(out)


def key_forms() -> list[tuple[str, str]]:
    """보유 키로 만들 수 있는 두 형태를 모두 돌려준다. [(이름, 값), ...]

    포털 안내문도 "API 호출 조건에 따라 인증키가 적용되는 방식이 다를 수
    있으니 Encoding/Decoding 키를 적용하면서 구동되는 키를 쓰라"고 한다.
    즉 어느 쪽이 맞는지는 찔러봐야 안다는 뜻이다. 그래서 둘 다 준비한다.
    """
    raw = SERVICE_KEY.strip()
    if "%" in raw:                       # Encoding 키를 갖고 있는 경우
        return [("Encoding(그대로)", raw),
                ("Decoding(풀어서)", unquote(raw))]
    return [("Decoding(인코딩해서)", quote(raw, safe="")),
            ("Decoding(그대로)", raw)]


def diagnose() -> None:
    """엔드포인트 2종 × 키 형태 2종을 전부 찔러보고 되는 조합을 찾는다.

    [왜 이렇게까지] 공공 API는 문서·콘솔·실제 동작이 서로 어긋나는 일이
    흔하다. 어느 게 맞는지 추측하며 하나씩 고치는 것보다, 가능한 조합을
    한 번에 다 시험해 표로 보는 편이 훨씬 빠르다. 조합이 4개뿐이라
    몇 초면 끝난다.

    찾은 조합은 전역 ENDPOINT와 _KEY_ENC에 저장되어 이후 호출에 쓰인다.
    """
    global ENDPOINT
    print("=" * 72)
    print("연결 진단 — 엔드포인트 × 키 형태 조합 시험")
    print("=" * 72)

    winner = None
    for ep in ENDPOINTS:
        for label, key in key_forms():
            url = (f"{ep}?serviceKey={key}"
                   f"&LAWD_CD=11680&DEAL_YMD=202606&pageNo=1&numOfRows=5")
            tag = f"{ep.split('/')[-1]:32s} + {label:22s}"
            try:
                r = requests.get(url, timeout=15)
            except Exception as e:
                print(f"  ✗ {tag} 연결실패 {e}")
                continue

            if r.status_code != 200:
                print(f"  ✗ {tag} HTTP {r.status_code}")
                continue
            try:
                root = ET.fromstring(r.text)
            except ET.ParseError:
                print(f"  ✗ {tag} XML 아님 ({r.text[:60]!r})")
                continue

            code = (root.findtext(".//resultCode") or "").strip()
            n = len(list(root.iter("item")))
            if code in ("", "00", "000"):
                print(f"  ✓ {tag} 성공 — {n}건")
                if winner is None:
                    winner = (ep, key)
            else:
                msg = ERROR_MSG.get(code.zfill(2), root.findtext(".//resultMsg"))
                print(f"  ✗ {tag} 오류 {code} — {msg}")

    print("=" * 72)
    if winner is None:
        raise SystemExit(
            "되는 조합이 없습니다.\n"
            " · 마이페이지에서 해당 API의 상태가 '승인'인지 확인\n"
            " · 마이페이지의 End Point 문자열을 ENDPOINTS 맨 앞에 넣어보기\n"
        )
    ENDPOINT = winner[0]
    globals()["_KEY_ENC"] = winner[1]
    print(f"사용할 엔드포인트: {ENDPOINT}")
    print(f"사용할 키 형태  : {'…' + winner[1][-12:]}\n")


def build_url(**params) -> str:
    """인증키를 정확히 '한 번만' 인코딩한 완성 URL을 만든다.

    [왜 params= 를 안 쓰나] requests에 딕셔너리로 넘기면 requests가
    모든 값을 인코딩한다. 그런데 Encoding 키는 이미 인코딩된 상태라
    %2F → %252F 로 한 번 더 감싸져서 서버가 키를 못 알아본다(403).
    키 안에 '%'가 있으면 이미 인코딩된 것으로 보고 그대로 두고,
    없으면 원본이므로 한 번만 인코딩한다. 두 형태 모두 통한다.
    """
    key_enc = globals().get("_KEY_ENC")
    if key_enc is None:                      # diagnose() 전이면 기본 규칙
        k = SERVICE_KEY.strip()
        key_enc = k if "%" in k else quote(k, safe="")
    rest = urlencode(params)
    return f"{ENDPOINT}?serviceKey={key_enc}&{rest}"


def fetch_month(lawd_cd: str, deal_ymd: str) -> list[dict]:
    """한 구·한 달치 전체 거래.

    [왜 while 루프] 한 페이지에 다 안 올 수 있다. totalCount와 비교하며
    끝까지 받아야 누락이 없다. "보통은 한 페이지면 충분"에 기대면
    조용히 데이터가 새고, 에러가 안 나서 몇 주 뒤에나 알아챈다.
    """
    rows, page = [], 1
    while True:
        url = build_url(LAWD_CD=lawd_cd, DEAL_YMD=deal_ymd,
                        pageNo=page, numOfRows=500)
        try:
            r = requests.get(url, timeout=20)
            r.raise_for_status()
        except requests.HTTPError:
            # [왜 여기서 즉시 중단하나] 403은 키 문제라 다음 달에도 똑같이
            # 실패한다. 그대로 두면 180번을 헛돌며 화면만 채운다.
            # 회복 불가능한 오류는 빨리 죽는 게 낫다(fail fast).
            if r.status_code == 403:
                raise SystemExit(
                    "\n[403 Forbidden] 인증키가 서버에 거부되었습니다.\n"
                    "가능성 높은 순서:\n"
                    " 1) 키 앞뒤 공백·줄바꿈 → .strip() 확인\n"
                    " 2) 활용신청이 아직 '승인' 전 (data.go.kr 마이페이지)\n"
                    " 3) 신청한 API와 다른 API를 호출 중\n"
                    f"실제 호출 URL: {url[:120]}...\n"
                )
            print(f"  [!] {lawd_cd} {deal_ymd} p{page} HTTP {r.status_code}")
            break
        except Exception as e:
            print(f"  [!] {lawd_cd} {deal_ymd} p{page} 요청 실패: {e}")
            break

        # 인증키가 틀리면 XML이 아니라 HTML 에러 페이지가 온다.
        # 그대로 파싱하면 원인 모를 예외가 나므로 응답 앞부분을 찍어준다.
        try:
            root = ET.fromstring(r.text)
        except ET.ParseError:
            print(f"  [!] {lawd_cd} {deal_ymd} 응답이 XML이 아닙니다:")
            print("      ", r.text[:300].replace("\n", " "))
            break

        code = (root.findtext(".//resultCode") or "").strip()
        if code not in ("", "00", "000"):
            hint = ERROR_MSG.get(code.zfill(2), root.findtext(".//resultMsg"))
            if code.zfill(2) != "03":          # 데이터 없음은 조용히 넘어간다
                print(f"  [!] {lawd_cd} {deal_ymd} 오류 {code}: {hint}")
            break

        page_rows = [
            {c.tag: (c.text or "").strip() for c in item}
            for item in root.iter("item")
        ]
        rows.extend(page_rows)

        total = int(root.findtext(".//totalCount") or 0)
        if len(rows) >= total or not page_rows:
            break
        page += 1
        time.sleep(SLEEP_SEC)

    return rows


def collect() -> pd.DataFrame:
    """설정한 구 × 월을 전부 돌아 원본 응답을 한 DataFrame으로 모은다."""
    months = month_list(YEARS_BACK)
    n = len(REGIONS) * len(months)
    print(f"수집 시작 — {len(REGIONS)}개 구 × {len(months)}개월 ≈ {n}회 호출\n")

    all_rows = []
    for cd, name in REGIONS.items():
        for i, ym in enumerate(months, 1):
            got = fetch_month(cd, ym)
            for g in got:
                g["_region"] = name        # 응답에 구 이름이 없으므로 직접 붙인다
            all_rows.extend(got)
            if i % 12 == 0:
                print(f"  {name} … {i}/{len(months)}개월, 누적 {len(all_rows):,}건")
            time.sleep(SLEEP_SEC)
        print(f"[{name}] 완료\n")

    if not all_rows:
        raise SystemExit(
            "거래가 0건입니다. 위 오류 메시지를 확인하세요.\n"
            " · 20번 → 활용승인 대기 중\n"
            " · 30번 → Encoding 키를 넣었을 가능성 (Decoding 키를 쓸 것)\n"
            " · 오류가 없는데 0건이면 → 신규 API가 과거 데이터를 주는지 확인 필요"
        )
    return pd.DataFrame(all_rows)


# ═══════════════════════════════════════════════════════════════
# 2. 정리 — 원본을 다루기 좋은 표로
# ═══════════════════════════════════════════════════════════════
def pick(df: pd.DataFrame, *candidates: str) -> pd.Series:
    """후보 컬럼명 중 실제 존재하는 첫 번째.

    기술문서 참고장에 '구 API ↔ 신 API 컬럼명 대조표'가 실려 있다.
    (aptname→aptNm, reqgbn→dealingGbn, rdealerlawdnm→estateAgentSggNm …)
    둘 다 후보로 두면 어느 버전이 와도 견딘다. 한쪽만 박아두면
    어느 날 전부 NaN이 되는데 에러도 안 나서 알아채기 어렵다.
    """
    for c in candidates:
        if c in df.columns:
            return df[c]
    print(f"  [주의] 컬럼 없음: {candidates}")
    return pd.Series([None] * len(df), index=df.index)


def tidy(raw: pd.DataFrame) -> pd.DataFrame:
    """거래 한 건 = 한 행. 이후 집계가 쓰는 컬럼을 여기서 다 만든다."""
    df = pd.DataFrame(index=raw.index)
    df["region"] = raw["_region"]
    df["dong"] = pick(raw, "umdNm", "umdnm")
    df["complex"] = pick(raw, "aptNm", "aptname")
    df["jibun"] = pick(raw, "jibun")
    df["year"] = pd.to_numeric(pick(raw, "dealYear", "dealyear"), errors="coerce")
    df["month"] = pd.to_numeric(pick(raw, "dealMonth", "dealmonth"), errors="coerce")
    df["day"] = pd.to_numeric(pick(raw, "dealDay", "dealday"), errors="coerce")
    df["area"] = pd.to_numeric(pick(raw, "excluUseAr", "excluusear"), errors="coerce")
    df["floor"] = pd.to_numeric(pick(raw, "floor"), errors="coerce")
    df["build_year"] = pd.to_numeric(pick(raw, "buildYear", "buildyear"), errors="coerce")

    # 거래금액은 "125,000" 같은 문자열(만원 단위)
    amt = pick(raw, "dealAmount", "dealamount").astype(str)
    df["price"] = pd.to_numeric(amt.str.replace(",", "").str.strip(), errors="coerce")

    # 신고자료에 함께 실리는 구분 항목들. 값은 그대로 옮기고 해석하지 않는다.
    df["deal_type"] = pick(raw, "dealingGbn", "reqgbn").astype(str).str.strip()
    df["agent_sgg"] = pick(raw, "estateAgentSggNm", "rdealerlawdnm").astype(str).str.strip()
    df["seller"] = pick(raw, "slerGbn", "slergbn").astype(str).str.strip()
    df["buyer"] = pick(raw, "buyerGbn", "buyergbn").astype(str).str.strip()
    df["rgst_date"] = pick(raw, "rgstDate", "rgstdate").astype(str).str.strip()

    # 해제(취소)된 거래 제외.
    # 남겨두면 거래량이 부풀고, 신고가 찍고 취소한 건이 가격을 위로 끌어올린다.
    cancel = pick(raw, "cdealType", "cdealtype").astype(str)
    df = df[~cancel.str.contains("O", na=False)]

    df = df.dropna(subset=["complex", "price", "area"])
    df["ym"] = df["year"].astype(int) * 100 + df["month"].astype(int)

    # 평당가 — 같은 단지라도 20평과 40평은 총액이 다르다. 단위면적당으로
    # 맞춰야 '면적 차이'를 '가격 차이'로 오독하지 않는다.
    df["price_per_py"] = df["price"] / (df["area"] / 3.3058)

    # 면적 구간 — 10㎡ 단위로 묶는다. 같은 단지라도 평형이 여럿이라
    # 묶지 않고 비교하면 평형 차이가 가격 분산으로 잡힌다.
    df["area_bin"] = (df["area"] // 10 * 10).astype(int)

    # 계약일 → 등기일 소요 날수. 값이 없거나 형식이 다르면 None.
    def days_to_reg(r):
        try:
            d0 = datetime(int(r["year"]), int(r["month"]), int(r["day"]))
            d1 = datetime.strptime(r["rgst_date"], "%y.%m.%d")
            return (d1 - d0).days
        except Exception:
            return None
    df["days_to_reg"] = df.apply(days_to_reg, axis=1)

    # 중개사 소재지가 거래가 일어난 구와 다른가
    df["agent_out"] = (
        df["agent_sgg"].notna()
        & (df["agent_sgg"] != "")
        & ~df.apply(lambda r: str(r["region"]) in str(r["agent_sgg"]), axis=1)
    )

    return df.reset_index(drop=True)


# ═══════════════════════════════════════════════════════════════
# 3. 집계
# ═══════════════════════════════════════════════════════════════
def brokerage_rate(p_eok: float) -> float:
    """주택 매매 중개보수 상한요율(편측). 인자는 억원 단위 거래가격.

    공인중개사법 시행규칙과 서울시 조례가 정한 거래금액 구간별 상한이다.
    아래 summarize_complex()에서 단지별 중개시장 규모를 어림잡는 데만 쓴다.
    상한이므로 실제 체결되는 보수와는 다르다.
    """
    if p_eok < 0.5:  return 0.006
    if p_eok < 2:    return 0.005
    if p_eok < 9:    return 0.004
    if p_eok < 12:   return 0.005
    if p_eok < 15:   return 0.006
    return 0.007


def price_cv(df: pd.DataFrame, min_n: int = 5) -> pd.Series:
    """단지별 가격 변동계수 — 같은 단지·면적대 안에서 평당가가 흩어진 정도.

    [왜 표준편차가 아니라 변동계수인가] 표준편차는 단위가 붙어 있어
    거래가가 높은 단지가 무조건 커 보인다. 평균으로 나누면
    「평균 대비 몇 퍼센트 흩어져 있는가」가 되어 40억 단지와 15억 단지를
    같은 잣대로 비교할 수 있다.

    [주의] 이 값은 층·향·타입·수리 상태를 보정하지 않은 것이다.
    값이 크다는 것이 「설명 없이 흩어져 있다」는 뜻인지 「층·향에 따른
    차이가 그만큼 크다」는 뜻인지는 이 값만으로 구분할 수 없다.
    구분하려면 아래 floor_adjusted_dispersion()을 쓴다.
    """
    cv = (df.groupby(["region", "dong", "complex", "area_bin"])["price_per_py"]
            .agg(["mean", "std", "size"]))
    cv = cv[cv["size"] >= min_n]          # 표본이 적으면 분산이 우연이다
    cv["cv"] = cv["std"] / cv["mean"]
    return cv.groupby(["region", "dong", "complex"])["cv"].mean().round(4)


def floor_adjusted_dispersion(df: pd.DataFrame, min_n: int = 20) -> pd.DataFrame:
    """층 효과를 걷어낸 뒤 남는 가격 분산.

    [무엇을 하는가] 단지·면적대 안에서 평당가를 층에 대해 1차 회귀시키고,
    회귀선으로 설명되지 않는 부분(잔차)의 흩어짐을 다시 잰다.

        평당가 = a × 층 + b + 잔차

    [무엇을 알 수 있는가]
        cv_raw    보정 전 변동계수 (price_cv와 같은 계산)
        cv_resid  층 효과를 뺀 뒤 남은 변동계수
        r2        층이 평당가 변동의 몇 %를 설명하는가 (0~1)
        층당_만원  층이 한 층 오를 때 평당가가 얼마나 오르는가

    cv_resid가 cv_raw와 비슷하면 층으로 설명되지 않는 흩어짐이 크다는 뜻이고,
    많이 줄어들면 그 단지의 가격차는 대부분 층 차이였다는 뜻이다.

    [왜 min_n이 5가 아니라 20인가] 회귀는 단순 분산보다 표본이 더 필요하다.
    점 5개로 직선을 그으면 기울기가 우연에 크게 흔들린다.

    [한계] 향·타입·수리 상태는 실거래 자료에 없어 보정할 수 없다.
    여기서 걷어내는 것은 층 하나뿐이다.
    """
    d = df.dropna(subset=["floor", "price_per_py"]).copy()
    rows = []
    for (region, dong, cx, ab), g in d.groupby(["region", "dong", "complex", "area_bin"]):
        if len(g) < min_n:
            continue
        y = g["price_per_py"].to_numpy(dtype=float)
        x = g["floor"].to_numpy(dtype=float)
        if x.std() == 0 or y.mean() == 0:      # 층이 전부 같으면 회귀 불가
            continue

        slope, intercept = np.polyfit(x, y, 1)
        resid = y - (slope * x + intercept)

        mean_y = y.mean()
        ss_tot = ((y - mean_y) ** 2).sum()
        rows.append({
            "region": region, "dong": dong, "complex": cx, "area_bin": ab,
            "n": len(g),
            "cv_raw": y.std(ddof=1) / mean_y,
            "cv_resid": resid.std(ddof=1) / mean_y,
            "r2": (1 - (resid ** 2).sum() / ss_tot) if ss_tot else np.nan,
            "층당_만원": slope,
        })

    if not rows:
        return pd.DataFrame()

    out = pd.DataFrame(rows)
    # 단지 안에 면적대가 여럿이면 표본 수로 가중해 평균낸다.
    # 단순 평균을 내면 거래가 서너 건뿐인 면적대가 큰 면적대와 같은 무게를 갖는다.
    def wmean(s, w):
        return np.average(s, weights=w) if len(s) else np.nan
    agg = out.groupby(["region", "dong", "complex"]).apply(
        lambda g: pd.Series({
            "표본수": int(g["n"].sum()),
            "면적대수": len(g),
            "cv_raw": wmean(g["cv_raw"], g["n"]),
            "cv_resid": wmean(g["cv_resid"], g["n"]),
            "r2": wmean(g["r2"], g["n"]),
            "층당_만원": wmean(g["층당_만원"], g["n"]),
        }), include_groups=False)
    agg["보정후_잔존율"] = (agg["cv_resid"] / agg["cv_raw"])
    return agg.round(4).sort_values("cv_resid", ascending=False)


def summarize_complex(df: pd.DataFrame) -> pd.DataFrame:
    """단지별 집계."""
    n_months = df["ym"].nunique()
    g = df.groupby(["region", "dong", "complex"])

    out = pd.DataFrame({
        "거래건수": g.size(),
        # 연 단위로 정규화 — 수집 기간을 바꿔도 같은 잣대가 된다
        "연평균거래": (g.size() / (n_months / 12)).round(1),
        # 중위값 — 초고가 한 건에 끌려가지 않는다
        "중위가_억": (g["price"].median() / 10000).round(2),
        "건축년도": g["build_year"].median(),
        # 아래 넷은 신고자료의 구분 항목을 그대로 집계한 것이다.
        "법인매수율": g["buyer"].apply(lambda s: (s == "법인").mean()).round(3),
        "직거래율": g["deal_type"].apply(lambda s: s.str.contains("직거래").mean()).round(3),
        "관외중개사율": g["agent_out"].mean().round(3),
        "등기소요일_중위": g["days_to_reg"].median(),
    })

    out["가격변동계수"] = price_cv(df)

    # 거래 한 건의 한쪽 당사자 기준으로 중개보수 상한을 적용한 어림값이다.
    # 상한이고 협의 결과가 아니므로 실제 금액과 다르다.
    out["편측중개보수_만원"] = out["중위가_억"].apply(
        lambda e: round(e * 10000 * brokerage_rate(e)) if pd.notna(e) else None
    )
    out["연간_시장규모_억"] = (
        out["연평균거래"] * out["편측중개보수_만원"] / 10000
    ).round(2)

    out = out[out["거래건수"] >= MIN_DEALS]
    return out.sort_values("연간_시장규모_억", ascending=False)


def summarize_region(df: pd.DataFrame, comp: pd.DataFrame) -> pd.DataFrame:
    """구별 집계."""
    n_months = df["ym"].nunique()
    g = df.groupby("region")
    out = pd.DataFrame({
        "총거래": g.size(),
        "연평균거래": (g.size() / (n_months / 12)).round(0),
        "중위가_억": (g["price"].median() / 10000).round(2),
        "거래단지수": g["complex"].nunique(),
        "법인매수율": g["buyer"].apply(lambda s: (s == "법인").mean()).round(3),
        "직거래율": g["deal_type"].apply(lambda s: s.str.contains("직거래").mean()).round(3),
        "관외중개사율": g["agent_out"].mean().round(3),
        "등기소요일_중위": g["days_to_reg"].median(),
    })
    # 거래가 상위 단지에 몰려 있는지, 넓게 퍼져 있는지
    top10 = (comp.reset_index().sort_values("거래건수", ascending=False)
             .groupby("region").head(10).groupby("region")["거래건수"].sum())
    out["상위10단지_집중도"] = (top10 / out["총거래"]).round(3)
    return out


def monthly_trend(df: pd.DataFrame) -> pd.DataFrame:
    """월별·구별 거래건수. 규제 시행 시점 전후를 보려면 이 표를 쓴다."""
    return (df.pivot_table(index="ym", columns="region",
                           values="price", aggfunc="size")
              .fillna(0).astype(int))


# ═══════════════════════════════════════════════════════════════
def main():
    if "여기에" in SERVICE_KEY:
        raise SystemExit("SERVICE_KEY(또는 환경변수 MOLIT_KEY)를 먼저 설정하세요.")

    diagnose()

    raw = collect()
    raw.to_csv("00_raw_api.csv", index=False, encoding="utf-8-sig")

    df = tidy(raw)
    df.to_csv("01_deals.csv", index=False, encoding="utf-8-sig")
    print(f"정리 완료 — {len(df):,}건 ({df['ym'].min()} ~ {df['ym'].max()})\n")

    comp = summarize_complex(df)
    comp.to_csv("02_complex.csv", encoding="utf-8-sig")

    reg = summarize_region(df, comp)
    reg.to_csv("03_region.csv", encoding="utf-8-sig")

    trend = monthly_trend(df)
    trend.to_csv("04_trend.csv", encoding="utf-8-sig")

    floor = floor_adjusted_dispersion(df)
    if not floor.empty:
        floor.to_csv("05_floor.csv", encoding="utf-8-sig")

    print("=" * 90)
    print("구별 요약")
    print("=" * 90)
    print(reg.to_string())

    print("\n" + "=" * 90)
    print("시장규모 상위 25개 단지")
    print("=" * 90)
    print(comp[["연평균거래", "중위가_억", "연간_시장규모_억", "가격변동계수",
                "법인매수율", "직거래율", "관외중개사율"]]
          .head(25).to_string())

    if not floor.empty:
        print("\n" + "=" * 90)
        print("층 보정 후에도 남는 가격 분산 상위 20개 단지")
        print("  cv_raw   보정 전 변동계수")
        print("  cv_resid 층 효과를 뺀 뒤 남은 변동계수")
        print("  r2       층이 평당가 변동의 몇 %를 설명하는가")
        print("=" * 90)
        print(floor.head(20).to_string())

    print("\n" + "=" * 90)
    print("월별 거래량 (최근 18개월)")
    print("=" * 90)
    print(trend.tail(18).to_string())

    print("\n저장: 00_raw_api / 01_deals / 02_complex / 03_region / "
          "04_trend / 05_floor .csv")


if __name__ == "__main__":
    main()


# ═══════════════════════════════════════════════════════════════
# 개정 이력
# ═══════════════════════════════════════════════════════════════
# v7  정비사업 단계로 거래를 분류하던 기능(WATCH·ZONES·summarize_zone)을
#     제거. 이 자료로 알 수 없는 것을 코드가 단정하고 있었다.
#     집계 결과를 해석하던 주석도 사실 서술로 바꿨다.
#     floor_adjusted_dispersion() 추가 — 층 효과를 걷어낸 잔여 분산.
#     조회 대상을 5개 구 전체로 되돌림.
# v6  단지명 부분일치로 인한 오태깅 수정 — (동, 단지명) 쌍으로 조건 강화.
# v5  diagnose() 추가(엔드포인트×키 형태 전수 시험), 이중 인코딩 수정,
#     에러코드 해석 추가, 신고자료 구분 항목 4종 활용, 구 API 컬럼명 병행 지원.
