import re
from contextlib import asynccontextmanager
from typing import Dict, Optional

from bs4 import BeautifulSoup
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
import httpx

STORE_CONFIG = [
    {
        "key": "+plus ゆめタウン福山店",
        "pattern": re.compile(r"ゆめタウン\s*福山", re.IGNORECASE),
    },
    {
        "key": "+plus エミフルMASAKI店",
        # 支援半形 MASAKI 與日文全形 ＭＡＳＡＫＩ
        "pattern": re.compile(r"エミフル\s*(?:MASAKI|ＭＡＳＡＫＩ)", re.IGNORECASE),
    },
    {
        "key": "+plus minamoa広島店",
        # 支援 minamoa、全形 ｍｉｎａｍｏａ、片假名 ミナモア
        "pattern": re.compile(r"(?:minamoa|ｍｉｎａｍｏａ|ミナモア)\s*広島", re.IGNORECASE),
    },
    {
        "key": "+plus 広島本通店",
        # 支援 広島本通 與 広島本通り
        "pattern": re.compile(r"広島本通(?:り)?", re.IGNORECASE),
    },
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Referer": "https://www.palcloset.jp/",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}

http_client: httpx.AsyncClient = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client
    http_client = httpx.AsyncClient(headers=HEADERS, timeout=15.0, follow_redirects=True)
    yield
    await http_client.aclose()

app = FastAPI(title="3COINS Stock Proxy", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def determine_stock(element_soup_or_text) -> int:
    """
    精確判定特定儲存格/區塊是否有庫存：
    1 = 有庫存 (在庫あり / 残りわずか / ◎ / ○ / ▲)
    0 = 無庫存 (在庫なし / 完売 / × / 品切れ / 未知)
    """
    if not element_soup_or_text:
        return 0

    if hasattr(element_soup_or_text, "get_text"):
        text = element_soup_or_text.get_text(separator=" ", strip=True)
        img_alts = " ".join([img.get("alt", "") for img in element_soup_or_text.find_all("img") if img.get("alt")])
        # 同步抓取可能代表狀態的 CSS classes
        classes = " ".join([
            " ".join(tag.get("class", []))
            for tag in element_soup_or_text.find_all(attrs={"class": True})
        ])
        if element_soup_or_text.get("class"):
            classes += " " + " ".join(element_soup_or_text.get("class", []))
        combined = f"{text} {img_alts} {classes}".lower()
    else:
        combined = str(element_soup_or_text).lower()

    in_tokens = [
        "在庫あり", "残りわずか", "◎", "▲", "○",
        "stock_ok", "is-instock", "is-stock", "status-ok", "status-few",
        "icon_stock_few", "icon_stock_ok"
    ]
    out_tokens = [
        "在庫なし", "完売", "品切れ", "×", "販売終了",
        "stock_none", "is-outstock", "is-none", "status-none", "icon_stock_none"
    ]

    has_in = any(token in combined for token in in_tokens)
    has_out = any(token in combined for token in out_tokens)

    # 精準單元格判斷：有在席標記且無缺貨標記回傳 1；若同時出現則在席優先
    if has_in:
        return 1
    if has_out:
        return 0
    return 0

def find_target_column_index(table, target_mi: str) -> int:
    """
    在多 SKU 表格中，透過 thead 或表頭尋找符合 target_mi 的欄位索引
    """
    thead = table.find("thead")
    header_tr = thead.find("tr") if thead else table.find("tr")
    if not header_tr:
        return -1

    headers = header_tr.find_all(["th", "td"])
    # 假設第一個欄位是門市名稱，後續為規格直欄
    sku_headers = headers[1:] if len(headers) > 1 else headers

    for idx, th in enumerate(sku_headers):
        th_str = str(th)
        # 1. 直接比對 SKU ID
        if target_mi in th_str:
            return idx
        # 2. 檢查伺服器端標記的選中欄位 (URL 帶有 mi 時常會帶有 active/selected)
        th_classes = " ".join(th.get("class", []))
        if any(c in th_classes for c in ["selected", "active", "is-active", "current"]):
            return idx

    return -1

def parse_html_for_stores(html_content: str, target_mi: str) -> Dict[str, int]:
    results = {cfg["key"]: 0 for cfg in STORE_CONFIG}
    soup = BeautifulSoup(html_content, "html.parser")

    # 若頁面為分頁/區塊式（各 SKU 獨立 div），先鎖定該 SKU 區塊
    sku_block = (
        soup.find(attrs={"data-mi": target_mi})
        or soup.find(id=re.compile(re.escape(target_mi)))
        or soup.find("div", class_=re.compile(re.escape(target_mi)))
    )
    search_root = sku_block if sku_block else soup
    tables = search_root.find_all("table")

    for cfg in STORE_CONFIG:
        store_key = cfg["key"]
        pattern = cfg["pattern"]
        found_status = None

        # 策略 1: 表格矩陣精確比對 (專門處理多規格商品)
        for table in tables:
            store_node = table.find(string=pattern)
            if not store_node:
                continue

            row = store_node.find_parent("tr")
            if not row:
                continue

            tds = row.find_all("td")
            if not tds:
                continue

            if len(tds) == 1:
                # 單一規格商品
                found_status = determine_stock(tds[0])
                break
            else:
                # 多規格商品：定位 target_mi 所屬欄位
                target_col_idx = find_target_column_index(table, target_mi)
                if 0 <= target_col_idx < len(tds):
                    found_status = determine_stock(tds[target_col_idx])
                    break
                else:
                    # 表頭無明確標記時，檢查個別 td 內是否有 mi 或 selected 標記
                    for td in tds:
                        td_str = str(td)
                        td_classes = " ".join(td.get("class", []))
                        if target_mi in td_str or any(c in td_classes for c in ["selected", "active", "is-active"]):
                            found_status = determine_stock(td)
                            break
                    if found_status is not None:
                        break

        # 策略 2: 列表結構容器比對 (非 table 時向上尋找 row/item 容器，避開內層單純的店名 div)
        if found_status is None:
            text_node = search_root.find(string=pattern)
            if text_node:
                curr = text_node.parent
                container = None
                while curr and curr.name not in ["body", "html"]:
                    if curr.name in ["tr", "li", "dl"]:
                        container = curr
                        break
                    # 若為 div，必須是包覆整個門市項目的外層容器
                    if curr.name == "div" and any(k in " ".join(curr.get("class", [])) for k in ["store", "shop", "item", "row", "stock"]):
                        container = curr
                        break
                    curr = curr.parent

                if container:
                    found_status = determine_stock(container)

        # 策略 3: 滑動視窗備援 (僅掃描店名後方，避免截取到上方說明圖例的「在庫なし」)
        if found_status is None:
            match = pattern.search(html_content)
            if match:
                pos = match.start()
                # 專注於店名出現後的 300 個字元
                window_html = html_content[pos: min(len(html_content), pos + 300)]
                plain = re.sub(r"<[^>]+>", " ", window_html)
                clean_text = re.sub(r"\s+", " ", plain).strip()
                found_status = determine_stock(clean_text)

        results[store_key] = found_status if found_status is not None else 0

    return results

@app.get("/api/stock")
async def get_stock(mi: str = Query(..., description="PAL CLOSET 商品管理 ID (mi)")):
    url = f"https://www.palcloset.jp/addons/pal/store_stock/?mi={mi}&b=3coins"

    try:
        resp = await http_client.get(url)
        if resp.status_code != 200:
            return {"status": "error", "mi": mi, "data": {c["key"]: 0 for c in STORE_CONFIG}}

        # 將 target_mi 帶入解析邏輯
        stock_results = parse_html_for_stores(resp.text, target_mi=mi)
        return {"status": "success", "mi": mi, "data": stock_results}

    except httpx.RequestError as e:
        return {"status": "error", "mi": mi, "message": f"Network error: {str(e)}", "data": {c["key"]: 0 for c in STORE_CONFIG}}
    except Exception as e:
        return {"status": "error", "mi": mi, "message": str(e), "data": {c["key"]: 0 for c in STORE_CONFIG}}
