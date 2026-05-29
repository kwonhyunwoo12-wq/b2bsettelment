import io
import os
import re
import sqlite3
import zipfile
from datetime import datetime

import pandas as pd
import streamlit as st
from rapidfuzz import fuzz, process

APP_TITLE = "터블/키친플래그 B2B 월정산 자동화"
BASE_DIR = os.path.dirname(__file__)
DB_PATH = os.path.join(BASE_DIR, "b2b_settlement.db")
SEED_PRODUCT_PATH = os.path.join(BASE_DIR, "seed_data", "product_master.csv")
DEFAULT_SHIPPING_FEE = 3000

REQUIRED_RAW_COLS = ["배송사", "송장번호", "파트너사", "주문일", "주문 상품명", "수량", "수취인", "수취인 주소"]
OUTPUT_COLS = ["배송사", "송장번호", "파트너사", "주문일", "주문 상품명", "수량", "공급가", "배송비", "상품금액", "총액", "수취인", "수취인 주소"]
MATCH_INFO_COLS = ["매칭상품명", "매칭점수", "매칭방식"]
PARTNER_MATCH_INFO_COLS = ["정산거래처명", "파트너매칭점수", "파트너매칭방식"]
DEFAULT_MATCH_THRESHOLD = 88
DEFAULT_PARTNER_MATCH_THRESHOLD = 75
PRODUCT_COLS = ["source_product_name", "standard_product_name", "brand", "supply_price", "carton_qty", "shipping_fee", "memo"]
PARTNER_COLS = ["partner", "display_partner", "email", "include_yn", "default_shipping_fee", "memo"]
EXCEPTION_COLS = ["partner", "source_product_name", "exception_supply_price", "memo"]


def normalize_text(v):
    if pd.isna(v):
        return ""
    return re.sub(r"\s+", " ", str(v).strip())


def canonical_product_name(v):
    """상품명 유사도 비교용 정규화: 공백/기호/괄호 차이를 최대한 제거합니다."""
    s = normalize_text(v).lower()
    s = s.replace("ｐ", "p").replace("－", "-").replace("–", "-").replace("—", "-")
    s = re.sub(r"\[[^\]]*\]", " ", s)  # [Tubble] 같은 태그 제거
    s = re.sub(r'[(){}<>\[\]/_+,:;|·ㆍ\\\'"“”‘’]', " ", s)
    s = re.sub(r"\s+", "", s)
    return s




def canonical_partner_name(v):
    """거래처명 유사도 비교용 정규화: (특판) 같은 태그/공백/기호 차이를 제거합니다."""
    s = normalize_text(v).lower()
    s = re.sub(r"\([^)]*\)", " ", s)  # (특판) 등 제거
    s = re.sub(r"\[[^\]]*\]", " ", s)
    s = re.sub(r"[^0-9a-z가-힣]", "", s)
    return s


def build_partner_matches(raw_partners, partner_df, threshold=DEFAULT_PARTNER_MATCH_THRESHOLD):
    """RAW 파트너사를 거래처마스터의 정산거래처명(display_partner)에 정확/유사 매칭합니다."""
    raw_partners = [normalize_text(x) for x in raw_partners if normalize_text(x)]
    partner_df = partner_df.copy()
    if partner_df.empty:
        return pd.DataFrame({
            "원본파트너사": sorted(set(raw_partners)),
            "정산거래처명": sorted(set(raw_partners)),
            "파트너매칭점수": 100,
            "파트너매칭방식": "신규등록",
        })
    partner_df["display_partner"] = partner_df["display_partner"].fillna("").map(normalize_text)
    partner_df.loc[partner_df["display_partner"].eq(""), "display_partner"] = partner_df["partner"].map(normalize_text)

    exact_by_raw = {normalize_text(r["partner"]): normalize_text(r["display_partner"]) for _, r in partner_df.iterrows()}
    choices = []
    choice_to_display = {}
    for display in sorted(set(partner_df["display_partner"].dropna().map(normalize_text))):
        can = canonical_partner_name(display)
        if can:
            choices.append(can)
            choice_to_display.setdefault(can, display)

    rows = []
    for raw in sorted(set(raw_partners)):
        raw_norm = normalize_text(raw)
        raw_can = canonical_partner_name(raw_norm)
        display = raw_norm
        score = 0
        mtype = "신규등록"
        if raw_norm in exact_by_raw:
            display, score, mtype = exact_by_raw[raw_norm], 100, "마스터일치"
        elif choices:
            hit = process.extractOne(raw_can, choices, scorer=fuzz.WRatio)
            if hit and hit[1] >= threshold:
                display, score, mtype = choice_to_display[hit[0]], int(round(hit[1])), "유사거래처묶음"
            elif hit:
                display, score, mtype = raw_norm, int(round(hit[1])), "신규등록"
        rows.append({
            "원본파트너사": raw_norm,
            "정산거래처명": display,
            "파트너매칭점수": score,
            "파트너매칭방식": mtype,
        })
    return pd.DataFrame(rows)


def build_product_matches(raw_products, prod_df, threshold=DEFAULT_MATCH_THRESHOLD):
    """RAW 상품명을 상품마스터에 정확/유사 매칭하여 매칭표를 만듭니다."""
    raw_products = [normalize_text(x) for x in raw_products if normalize_text(x)]
    prod_df = prod_df.copy()
    prod_df["_canon"] = prod_df["source_product_name"].map(canonical_product_name)
    exact_by_name = {normalize_text(x): normalize_text(x) for x in prod_df["source_product_name"].dropna()}
    canon_to_source = {}
    choices = []
    choice_to_source = {}
    for _, r in prod_df.iterrows():
        src = normalize_text(r["source_product_name"])
        can = r["_canon"]
        if not src or not can:
            continue
        canon_to_source.setdefault(can, src)
        choices.append(can)
        choice_to_source.setdefault(can, src)

    rows = []
    for raw in sorted(set(raw_products)):
        raw_norm = normalize_text(raw)
        raw_can = canonical_product_name(raw_norm)
        matched = ""
        score = 0
        mtype = "미매칭"
        if raw_norm in exact_by_name:
            matched, score, mtype = exact_by_name[raw_norm], 100, "정확일치"
        elif raw_can in canon_to_source:
            matched, score, mtype = canon_to_source[raw_can], 100, "정규화일치"
        elif choices:
            hit = process.extractOne(raw_can, choices, scorer=fuzz.WRatio)
            if hit and hit[1] >= threshold:
                matched, score, mtype = choice_to_source[hit[0]], int(round(hit[1])), "유사매칭"
            elif hit:
                matched, score, mtype = "", int(round(hit[1])), "미매칭"
        rows.append({
            "주문 상품명": raw_norm,
            "matched_source_product_name": matched,
            "매칭상품명": matched,
            "매칭점수": score,
            "매칭방식": mtype,
        })
    return pd.DataFrame(rows)


def clean_price(v):
    if pd.isna(v):
        return 0
    s = normalize_text(v)
    if s in ["", "-", "문의", "별도", "nan", "NaN"]:
        return 0
    s = re.sub(r"[^0-9.-]", "", s)
    if s in ["", ".", "-"]:
        return 0
    try:
        return int(round(float(s)))
    except Exception:
        return 0


def clean_qty(v):
    return clean_price(v)


def safe_filename(text):
    text = normalize_text(text) or "거래처명없음"
    return re.sub(r"[\\/:*?\"<>|]", "_", text)


def conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with conn() as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS product_master (
                source_product_name TEXT PRIMARY KEY,
                standard_product_name TEXT,
                brand TEXT,
                supply_price INTEGER DEFAULT 0,
                carton_qty INTEGER DEFAULT 0,
                shipping_fee INTEGER DEFAULT 3000,
                memo TEXT
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS partner_master (
                partner TEXT PRIMARY KEY,
                display_partner TEXT,
                email TEXT,
                include_yn TEXT DEFAULT 'Y',
                default_shipping_fee INTEGER DEFAULT 3000,
                memo TEXT
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS price_exception (
                partner TEXT,
                source_product_name TEXT,
                exception_supply_price INTEGER,
                memo TEXT,
                PRIMARY KEY (partner, source_product_name)
            )
            """
        )
        c.commit()
    seed_products_if_empty()


def read_table(table, cols):
    with conn() as c:
        try:
            df = pd.read_sql_query(f"SELECT * FROM {table}", c)
        except Exception:
            df = pd.DataFrame(columns=cols)
    for col in cols:
        if col not in df.columns:
            df[col] = ""
    return df[cols]


def replace_table(table, df, cols):
    clean = df.copy()
    for col in cols:
        if col not in clean.columns:
            clean[col] = ""
    clean = clean[cols]
    if table == "product_master":
        clean["source_product_name"] = clean["source_product_name"].map(normalize_text)
        clean["standard_product_name"] = clean["standard_product_name"].map(normalize_text)
        clean["brand"] = clean["brand"].map(normalize_text)
        clean["supply_price"] = clean["supply_price"].map(clean_price)
        clean["carton_qty"] = clean["carton_qty"].map(clean_qty)
        clean["shipping_fee"] = clean["shipping_fee"].map(clean_price).replace(0, DEFAULT_SHIPPING_FEE)
        clean = clean[clean["source_product_name"].ne("")]
    elif table == "partner_master":
        clean["partner"] = clean["partner"].map(normalize_text)
        clean["display_partner"] = clean["display_partner"].map(normalize_text)
        clean["email"] = clean["email"].map(normalize_text)
        clean["include_yn"] = clean["include_yn"].fillna("Y").astype(str).str.upper().str[:1].replace({"": "Y"})
        clean["default_shipping_fee"] = clean["default_shipping_fee"].map(clean_price).replace(0, DEFAULT_SHIPPING_FEE)
        clean = clean[clean["partner"].ne("")]
    elif table == "price_exception":
        clean["partner"] = clean["partner"].map(normalize_text)
        clean["source_product_name"] = clean["source_product_name"].map(normalize_text)
        clean["exception_supply_price"] = clean["exception_supply_price"].map(clean_price)
        clean = clean[clean["partner"].ne("") & clean["source_product_name"].ne("")]
    clean = clean.drop_duplicates(subset=[cols[0]] if table != "price_exception" else ["partner", "source_product_name"], keep="last")
    with conn() as c:
        clean.to_sql(table, c, if_exists="replace", index=False)
        c.commit()
    init_db()


def upsert_table(table, df, cols, key_cols):
    if df.empty:
        return
    base = read_table(table, cols)
    combined = pd.concat([base, df[cols]], ignore_index=True)
    combined = combined.drop_duplicates(subset=key_cols, keep="last")
    replace_table(table, combined, cols)


def seed_products_if_empty():
    with conn() as c:
        cnt = c.execute("SELECT COUNT(*) FROM product_master").fetchone()[0]
    if cnt == 0 and os.path.exists(SEED_PRODUCT_PATH):
        try:
            seed = pd.read_csv(SEED_PRODUCT_PATH)
            replace_table("product_master", seed, PRODUCT_COLS)
        except Exception:
            pass


def find_header_row(raw_df):
    for i in range(min(len(raw_df), 30)):
        vals = [str(x).replace("\n", "").strip() for x in raw_df.iloc[i].tolist()]
        joined = "|".join(vals)
        if "상품명" in joined and "공급가" in joined:
            return i
    return None


def parse_product_master_excel(file) -> pd.DataFrame:
    xl = pd.ExcelFile(file)
    rows = []
    for sheet in xl.sheet_names:
        raw = pd.read_excel(file, sheet_name=sheet, header=None)
        header_row = find_header_row(raw)
        if header_row is None:
            continue
        header = raw.iloc[header_row].astype(str).str.replace("\n", "", regex=False).str.strip().tolist()
        df = raw.iloc[header_row + 1 :].copy()
        df.columns = header
        product_col = next((c for c in df.columns if c == "상품명" or "상품명" in str(c)), None)
        price_col = next((c for c in df.columns if "공급가" in str(c)), None)
        carton_col = next((c for c in df.columns if "카톤수량" in str(c).replace(" ", "")), None)
        ship_col = next((c for c in df.columns if "배송비" in str(c)), None)
        if not product_col or not price_col:
            continue
        brand = "터블" if "터블" in sheet or "Tubble" in sheet else ("키친플래그" if "키친" in sheet or "Kitchen" in sheet else "")
        temp = pd.DataFrame()
        temp["source_product_name"] = df[product_col].map(normalize_text)
        temp["standard_product_name"] = temp["source_product_name"]
        temp["brand"] = brand
        temp["supply_price"] = df[price_col].map(clean_price)
        temp["carton_qty"] = df[carton_col].map(clean_qty) if carton_col else 0
        temp["shipping_fee"] = df[ship_col].map(clean_price).replace(0, DEFAULT_SHIPPING_FEE) if ship_col else DEFAULT_SHIPPING_FEE
        temp["memo"] = sheet
        temp = temp[temp["source_product_name"].ne("")]
        temp = temp[temp["supply_price"].gt(0)]
        rows.append(temp[PRODUCT_COLS])
    if not rows:
        return pd.DataFrame(columns=PRODUCT_COLS)
    out = pd.concat(rows, ignore_index=True)
    return out.drop_duplicates(subset=["source_product_name"], keep="last")


def read_raw_excel(file) -> pd.DataFrame:
    xl = pd.ExcelFile(file)
    frames = []
    for sheet in xl.sheet_names:
        df = pd.read_excel(file, sheet_name=sheet)
        df.columns = [str(c).replace("\n", "").strip() for c in df.columns]
        if set(REQUIRED_RAW_COLS).issubset(set(df.columns)):
            frames.append(df)
    if not frames:
        raise ValueError("필수 컬럼이 있는 시트를 찾지 못했습니다. 필요한 컬럼: " + ", ".join(REQUIRED_RAW_COLS))
    df = pd.concat(frames, ignore_index=True)
    for col in REQUIRED_RAW_COLS:
        if col not in df.columns:
            raise ValueError(f"필수 컬럼 누락: {col}")
    return df.copy()


def ensure_partners(raw_df, partner_threshold=DEFAULT_PARTNER_MATCH_THRESHOLD):
    raw_partners = sorted(raw_df["파트너사"].dropna().map(normalize_text).unique())
    partners_df = read_table("partner_master", PARTNER_COLS)
    existing_raw = set(partners_df["partner"].map(normalize_text).tolist())
    new_raw = [p for p in raw_partners if p and p not in existing_raw]
    if new_raw:
        # 기존 마스터 및 이번에 추가되는 거래처를 보며 비슷한 거래처는 같은 정산거래처명으로 자동 묶습니다.
        rows = []
        working = partners_df.copy()
        for p in new_raw:
            match = build_partner_matches([p], working, threshold=partner_threshold)
            display = match.iloc[0]["정산거래처명"] if len(match) else p
            score = match.iloc[0]["파트너매칭점수"] if len(match) else 0
            mtype = match.iloc[0]["파트너매칭방식"] if len(match) else "신규등록"
            rows.append({
                "partner": p,
                "display_partner": display,
                "email": "",
                "include_yn": "Y",
                "default_shipping_fee": DEFAULT_SHIPPING_FEE,
                "memo": f"RAW 업로드 시 자동 추가 / {mtype} / score={score}",
            })
            working = pd.concat([working, pd.DataFrame([rows[-1]])], ignore_index=True)
        df = pd.DataFrame(rows)
        upsert_table("partner_master", df[PARTNER_COLS], PARTNER_COLS, ["partner"])


def calculate_settlement(raw_df, match_threshold=DEFAULT_MATCH_THRESHOLD, partner_threshold=DEFAULT_PARTNER_MATCH_THRESHOLD):
    prod = read_table("product_master", PRODUCT_COLS)
    partners = read_table("partner_master", PARTNER_COLS)
    exc = read_table("price_exception", EXCEPTION_COLS)

    df = raw_df.copy()
    for col in ["배송사", "송장번호", "파트너사", "주문 상품명", "수취인", "수취인 주소"]:
        df[col] = df[col].map(normalize_text)
    df["원본 파트너사"] = df["파트너사"]
    df["수량"] = df["수량"].map(clean_qty)
    df["주문일"] = pd.to_datetime(df["주문일"], errors="coerce").dt.strftime("%Y-%m-%d")

    match_df = build_product_matches(df["주문 상품명"].dropna().unique(), prod, threshold=match_threshold)
    df = df.merge(match_df, how="left", on="주문 상품명")
    df = df.merge(prod[["source_product_name", "standard_product_name", "brand", "supply_price", "carton_qty", "shipping_fee"]],
                  how="left", left_on="matched_source_product_name", right_on="source_product_name")
    df = df.merge(partners[["partner", "display_partner", "include_yn", "default_shipping_fee"]],
                  how="left", left_on="파트너사", right_on="partner")
    df["display_partner"] = df["display_partner"].fillna(df["파트너사"]).map(normalize_text)

    # 파트너사 오타/표기차이는 거래처마스터의 display_partner(정산거래처명) 기준으로 묶습니다.
    partner_match_df = build_partner_matches(df["파트너사"].dropna().unique(), partners, threshold=partner_threshold)
    df = df.merge(partner_match_df, how="left", left_on="파트너사", right_on="원본파트너사")
    df["정산거래처명"] = df["정산거래처명"].fillna(df["display_partner"]).map(normalize_text)
    df["파트너매칭점수"] = df["파트너매칭점수"].fillna(100).astype(int)
    df["파트너매칭방식"] = df["파트너매칭방식"].fillna("마스터일치")

    # 예외단가는 정산거래처명 기준 우선, 없으면 RAW 파트너사 기준으로 한 번 더 찾습니다.
    exc_display = exc.rename(columns={"partner": "exc_partner_display", "source_product_name": "exc_product_display", "exception_supply_price": "exception_supply_price_display"})
    df = df.merge(exc_display[["exc_partner_display", "exc_product_display", "exception_supply_price_display"]],
                  how="left", left_on=["정산거래처명", "matched_source_product_name"], right_on=["exc_partner_display", "exc_product_display"])
    exc_raw = exc.rename(columns={"partner": "exc_partner_raw", "source_product_name": "exc_product_raw", "exception_supply_price": "exception_supply_price_raw"})
    df = df.merge(exc_raw[["exc_partner_raw", "exc_product_raw", "exception_supply_price_raw"]],
                  how="left", left_on=["원본 파트너사", "matched_source_product_name"], right_on=["exc_partner_raw", "exc_product_raw"])

    df["include_yn"] = df["include_yn"].fillna("Y").astype(str).str.upper().str[:1]
    df = df[df["include_yn"].eq("Y")].copy()

    df["공급가"] = df["exception_supply_price_display"].where(df["exception_supply_price_display"].notna(), df["exception_supply_price_raw"])
    df["공급가"] = df["공급가"].where(df["공급가"].notna(), df["supply_price"])
    df["파트너사"] = df["정산거래처명"]
    df["공급가"] = df["공급가"].map(clean_price)
    df["carton_qty"] = df["carton_qty"].map(clean_qty)
    df["default_shipping_fee"] = df["default_shipping_fee"].map(clean_price).replace(0, DEFAULT_SHIPPING_FEE)

    # 합배송 기준: 같은 업체 + 같은 주문일 + 같은 수취인 + 같은 주소
    df["_addr_norm"] = df["수취인 주소"].str.replace(r"\s+", "", regex=True).str.lower()
    df["_ship_group"] = df["파트너사"] + "|" + df["주문일"].fillna("") + "|" + df["수취인"] + "|" + df["_addr_norm"]
    df["_row_order"] = range(len(df))

    # 카톤수량 무료배송: 같은 배송묶음 안에서 동일 상품 수량 합이 카톤수량 이상이면 그 묶음 무료배송
    same_product_qty = df.groupby(["_ship_group", "주문 상품명"], dropna=False)["수량"].transform("sum")
    df["_free_by_carton"] = (df["carton_qty"] > 0) & (same_product_qty >= df["carton_qty"])
    group_free = df.groupby("_ship_group")["_free_by_carton"].transform("any")
    first_line = df.groupby("_ship_group")["_row_order"].rank(method="first").eq(1)
    df["배송비"] = 0
    df.loc[first_line & (~group_free), "배송비"] = df.loc[first_line & (~group_free), "default_shipping_fee"].astype(int)

    df["상품금액"] = df["수량"].astype(int) * df["공급가"].astype(int)
    df["총액"] = df["상품금액"] + df["배송비"].astype(int)
    df["오류"] = ""
    df.loc[df["matched_source_product_name"].fillna("").eq(""), "오류"] += "상품마스터 미매칭; "
    df.loc[df["공급가"].eq(0), "오류"] += "공급가 누락/0원; "
    df.loc[df["수량"].le(0), "오류"] += "수량 오류; "
    df.loc[df["주문일"].isna() | df["주문일"].eq("NaT"), "오류"] += "주문일 오류; "

    for c in OUTPUT_COLS:
        if c not in df.columns:
            df[c] = ""
    return df[OUTPUT_COLS + ["원본 파트너사", "standard_product_name", "brand", "carton_qty"] + MATCH_INFO_COLS + PARTNER_MATCH_INFO_COLS + ["오류"]].copy()


def excel_bytes(df, sheet_name="거래내역서"):
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        df[OUTPUT_COLS].to_excel(writer, index=False, sheet_name=sheet_name[:31])
        workbook = writer.book
        ws = writer.sheets[sheet_name[:31]]
        header_fmt = workbook.add_format({"bold": True, "bg_color": "#E7E6E6", "border": 1, "align": "center", "valign": "vcenter"})
        text_fmt = workbook.add_format({"border": 1, "valign": "top"})
        money_fmt = workbook.add_format({"num_format": "#,##0", "border": 1})
        date_fmt = workbook.add_format({"num_format": "yyyy-mm-dd", "border": 1})
        widths = {"배송사": 12, "송장번호": 18, "파트너사": 22, "주문일": 12, "주문 상품명": 46, "수량": 9, "공급가": 12, "배송비": 12, "상품금액": 12, "총액": 12, "수취인": 12, "수취인 주소": 48}
        for i, col in enumerate(OUTPUT_COLS):
            ws.write(0, i, col, header_fmt)
            fmt = money_fmt if col in ["수량", "공급가", "배송비", "상품금액", "총액"] else (date_fmt if col == "주문일" else text_fmt)
            ws.set_column(i, i, widths.get(col, 14), fmt)
        ws.freeze_panes(1, 0)
        ws.autofilter(0, 0, max(len(df), 1), len(OUTPUT_COLS)-1)
        total_row = len(df) + 2
        ws.write(total_row, 4, "합계", header_fmt)
        for col in ["수량", "배송비", "상품금액", "총액"]:
            idx = OUTPUT_COLS.index(col)
            letter = chr(ord("A") + idx)
            ws.write_formula(total_row, idx, f"=SUM({letter}2:{letter}{len(df)+1})", money_fmt)
    output.seek(0)
    return output.getvalue()


def summary_excel_bytes(settle_df):
    output = io.BytesIO()
    summary = settle_df.groupby("파트너사", as_index=False).agg(
        출고라인=("송장번호", "count"),
        총수량=("수량", "sum"),
        상품금액=("상품금액", "sum"),
        배송비=("배송비", "sum"),
        총액=("총액", "sum"),
    ).sort_values("총액", ascending=False)
    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        summary.to_excel(writer, index=False, sheet_name="거래처별요약")
        settle_df[OUTPUT_COLS + MATCH_INFO_COLS + ["오류"]].to_excel(writer, index=False, sheet_name="전체거래내역")
    output.seek(0)
    return output.getvalue()


def zip_partner_files(settle_df):
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("00_거래처별_요약_및_전체거래내역.xlsx", summary_excel_bytes(settle_df))
        errors = settle_df[settle_df["오류"].fillna("").ne("")]
        if not errors.empty:
            zf.writestr("99_오류확인.xlsx", excel_bytes(errors, "오류확인"))
        for partner, g in settle_df.groupby("파트너사", sort=True):
            month = "정산"
            valid_dates = g["주문일"].dropna().astype(str)
            if not valid_dates.empty:
                first = valid_dates.iloc[0]
                if len(first) >= 7:
                    y, m = first[:4], first[5:7]
                    month = f"{y}년{int(m)}월"
            zf.writestr(f"{month}_{safe_filename(partner)}_거래내역서.xlsx", excel_bytes(g, "거래내역서"))
    zbuf.seek(0)
    return zbuf.getvalue()


def settings_backup_bytes():
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        read_table("product_master", PRODUCT_COLS).to_excel(writer, index=False, sheet_name="product_master")
        read_table("partner_master", PARTNER_COLS).to_excel(writer, index=False, sheet_name="partner_master")
        read_table("price_exception", EXCEPTION_COLS).to_excel(writer, index=False, sheet_name="price_exception")
    output.seek(0)
    return output.getvalue()


def restore_settings(file):
    xl = pd.ExcelFile(file)
    if "product_master" in xl.sheet_names:
        replace_table("product_master", pd.read_excel(file, sheet_name="product_master"), PRODUCT_COLS)
    if "partner_master" in xl.sheet_names:
        replace_table("partner_master", pd.read_excel(file, sheet_name="partner_master"), PARTNER_COLS)
    if "price_exception" in xl.sheet_names:
        replace_table("price_exception", pd.read_excel(file, sheet_name="price_exception"), EXCEPTION_COLS)


def page_products():
    st.subheader("상품/표준 공급가 관리")
    st.caption("표준 단가표를 업로드하거나 화면에서 직접 수정합니다. source_product_name은 RAW의 '주문 상품명'과 매칭됩니다.")
    upload = st.file_uploader("표준 단가표 엑셀 업로드", type=["xlsx"], key="product_upload")
    if upload and st.button("표준 단가표 불러오기/병합", type="primary"):
        imported = parse_product_master_excel(upload)
        if imported.empty:
            st.error("상품명/공급가 컬럼을 찾지 못했습니다. 단가표 양식을 확인해 주세요.")
        else:
            upsert_table("product_master", imported, PRODUCT_COLS, ["source_product_name"])
            st.success(f"{len(imported):,}개 상품을 불러왔습니다.")
    prod = read_table("product_master", PRODUCT_COLS)
    edited = st.data_editor(prod, num_rows="dynamic", use_container_width=True, key="prod_editor", height=520)
    if st.button("상품마스터 저장"):
        replace_table("product_master", edited, PRODUCT_COLS)
        st.success("저장 완료")


def page_partners():
    st.subheader("거래처 관리")
    st.caption("정산 제외 업체는 include_yn을 N으로 변경하세요. 기본 배송비는 3,000원 기준입니다.")
    partners = read_table("partner_master", PARTNER_COLS)
    edited = st.data_editor(partners, num_rows="dynamic", use_container_width=True, key="partner_editor", height=520)
    if st.button("거래처 저장"):
        replace_table("partner_master", edited, PARTNER_COLS)
        st.success("저장 완료")


def page_exceptions():
    st.subheader("업체별 예외 공급가")
    st.caption("대부분은 표준 공급가를 쓰고, 네고단가가 있는 거래처/상품만 여기에 입력합니다. 예외 공급가가 표준 공급가보다 우선 적용됩니다.")
    exc = read_table("price_exception", EXCEPTION_COLS)
    edited = st.data_editor(exc, num_rows="dynamic", use_container_width=True, key="exception_editor", height=520)
    if st.button("예외단가 저장"):
        replace_table("price_exception", edited, EXCEPTION_COLS)
        st.success("저장 완료")


def page_backup():
    st.subheader("설정 백업/복원")
    st.warning("Streamlit Cloud 무료 배포 환경은 서버가 재시작되면 저장 파일이 초기화될 수 있습니다. 단가/거래처 수정 후에는 설정 백업 파일을 다운로드해 두세요.")
    st.download_button("현재 설정 백업 다운로드", data=settings_backup_bytes(), file_name="b2b_settlement_settings.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    restore = st.file_uploader("백업 설정 파일 복원", type=["xlsx"], key="restore")
    if restore and st.button("설정 복원 실행", type="primary"):
        restore_settings(restore)
        st.success("설정 복원 완료")
    st.divider()
    if st.button("전체 설정 초기화", type="secondary"):
        if os.path.exists(DB_PATH):
            os.remove(DB_PATH)
        init_db()
        st.success("초기화 완료")


def page_settlement():
    st.subheader("정산파일 생성")
    st.markdown("RAW 출고리스트를 업로드하면 공급가/배송비를 계산하고 업체별 거래내역서 엑셀을 ZIP으로 생성합니다.")
    with st.expander("상품명 자동매칭 설정", expanded=True):
        match_threshold = st.slider("유사매칭 기준점수", min_value=70, max_value=100, value=DEFAULT_MATCH_THRESHOLD, step=1,
                                    help="점수가 낮을수록 더 많이 자동매칭되지만 오매칭 가능성이 커집니다. 실무 권장값은 85~90점입니다.")
        st.caption("정확히 같은 상품명은 100점으로 바로 매칭하고, 공백/괄호/기호 차이는 정규화 후 매칭합니다. 그 외에는 가장 비슷한 상품명을 자동 추천·적용합니다.")
        partner_threshold = st.slider("파트너사 오타 자동묶음 기준점수", min_value=60, max_value=100, value=DEFAULT_PARTNER_MATCH_THRESHOLD, step=1,
                                      help="점수가 낮을수록 나행/니행, 지앤티/지엔티처럼 비슷한 거래처를 자동으로 같은 정산거래처명으로 묶습니다. 권장값은 75~85점입니다.")
    raw_upload = st.file_uploader("RAW 출고리스트 엑셀 업로드", type=["xlsx"], key="raw_upload")
    if not raw_upload:
        st.info("필수 컬럼: " + ", ".join(REQUIRED_RAW_COLS))
        return
    try:
        raw = read_raw_excel(raw_upload)
        ensure_partners(raw, partner_threshold=partner_threshold)
        result = calculate_settlement(raw, match_threshold=match_threshold, partner_threshold=partner_threshold)
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("정산 거래처", f"{result['파트너사'].nunique():,}개")
        c2.metric("출고 라인", f"{len(result):,}건")
        c3.metric("총 수량", f"{int(result['수량'].sum()):,}개")
        c4.metric("배송비", f"{int(result['배송비'].sum()):,}원")
        c5.metric("총 정산금액", f"{int(result['총액'].sum()):,}원")
        errors = result[result["오류"].fillna("").ne("")]
        if len(errors) > 0:
            st.error(f"확인 필요 데이터 {len(errors):,}건이 있습니다. 아래 오류 데이터부터 처리하세요.")
            st.dataframe(errors[OUTPUT_COLS + MATCH_INFO_COLS + PARTNER_MATCH_INFO_COLS + ["오류"]], use_container_width=True, height=300)
        else:
            st.success("오류 없이 계산되었습니다.")
        st.markdown("### 상품명 매칭 현황")
        match_summary = result.groupby(["주문 상품명", "매칭상품명", "매칭방식"], dropna=False, as_index=False).agg(라인수=("송장번호", "count"), 매칭점수=("매칭점수", "max"))
        st.dataframe(match_summary.sort_values(["매칭방식", "매칭점수"], ascending=[True, False]), use_container_width=True, height=260)
        st.markdown("### 파트너사 자동묶음 현황")
        partner_summary = result.groupby(["원본 파트너사", "정산거래처명", "파트너매칭방식"], dropna=False, as_index=False).agg(라인수=("송장번호", "count"), 파트너매칭점수=("파트너매칭점수", "max"))
        st.dataframe(partner_summary.sort_values(["파트너매칭방식", "파트너매칭점수"], ascending=[True, False]), use_container_width=True, height=220)
        st.markdown("### 거래처별 요약")
        summary = result.groupby("파트너사", as_index=False).agg(출고라인=("송장번호", "count"), 총수량=("수량", "sum"), 상품금액=("상품금액", "sum"), 배송비=("배송비", "sum"), 총액=("총액", "sum")).sort_values("총액", ascending=False)
        st.dataframe(summary, use_container_width=True, height=300)
        st.markdown("### 계산 결과 미리보기")
        st.dataframe(result[OUTPUT_COLS + MATCH_INFO_COLS + PARTNER_MATCH_INFO_COLS + ["오류"]].head(1000), use_container_width=True, height=420)
        st.download_button("업체별 거래내역서 ZIP 다운로드", data=zip_partner_files(result), file_name=f"B2B_업체별_거래내역서_{datetime.now().strftime('%Y%m%d_%H%M')}.zip", mime="application/zip", type="primary")
    except Exception as e:
        st.exception(e)


def app():
    st.set_page_config(page_title=APP_TITLE, layout="wide")
    init_db()
    st.title(APP_TITLE)
    st.caption("RAW 출고리스트 → 업체별 공급가/배송비 계산 → 거래처 제출용 거래내역서 엑셀 ZIP 생성")
    page = st.sidebar.radio("메뉴", ["정산파일 생성", "상품/표준 공급가", "업체별 예외 공급가", "거래처 관리", "설정 백업/복원"])
    if page == "정산파일 생성":
        page_settlement()
    elif page == "상품/표준 공급가":
        page_products()
    elif page == "업체별 예외 공급가":
        page_exceptions()
    elif page == "거래처 관리":
        page_partners()
    else:
        page_backup()


if __name__ == "__main__":
    app()
