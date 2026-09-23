import re
from contextlib import asynccontextmanager
from typing import Dict

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
        # 涵蓋半形、全形及僅含 MASAKI 的寫法
        "pattern": re.compile(r"エミフル\s*(?:MASAKI|ＭＡＳＡＫＩ)?|(?:MASAKI|ＭＡＳＡＫＩ)", re.IGNORECASE),
    },
    {
        "key": "+plus minamoa広島店",
        # ミナモア是廣島新站大樓專屬名詞，直接鎖定關鍵字，不受「広島」前後位置影響
        "pattern": re.compile(r"(?:minamoa|ｍｉｎａｍｏａ|ミナモア)", re.IGNORECASE),
    },
    {
        "key": "+plus 広島本通店",
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
    判定特定儲存格/區塊是否有庫存：
    1 = 有庫存 (在庫あり / 残りわずか / ◎ / ○ / ▲)
    0 = 無庫存 (在庫なし / 完売 / × / 品切れ)
    """
    if not element_soup_or_text:
        return 0

    if hasattr(element_soup_or_text, "get_text"):
        text = element_soup_or_text.get_text(separator=" ", strip=True)
        img_alts = " ".join([img.get("alt", "") for img in element_soup_or_text.find_all("img") if img.get("alt")])
        # 加入 img src 抓取，防止 icon 圖片無 alt 屬性
        img_srcs = " ".join([img.get("src", "") for img in element_soup_or_text.find_all("img") if img.get("src")])
        classes = " ".join([
            " ".join(tag.get("class", []))
            for tag in element_soup_or_text.find_all(attrs={"class": True})
        ])
        if element_soup_or_text.get("class"):
            classes += " " + " ".join(element_soup_or_text.get("class", []))
        combined = f"{text} {img_alts} {img_srcs} {classes}".lower()
    else:
        combined = str(element_soup_or_text).lower()

    # 明確的缺貨標記
    out_tokens = [
        "在庫なし", "完売", "品切れ", "×", "販売終了",
        "stock_none", "is-outstock", "status-none",
        "icon_stock_none", "stock-none", "status_none"
    ]
    # 明確的在席標記（移除模糊的 'is-stock'，避免誤中 is-stock-none）
    in_tokens = [
        "在庫あり", "残りわずか", "◎", "▲", "○",
        "stock_ok", "is-instock", "status-ok", "status-few",
        "icon_stock_few", "icon_stock_ok", "stock_few", "stock-few",
        "status_few", "status_ok"
    ]

    has_out = any(token in combined for token in out_tokens)
    has_in = any(token in combined for token in in_tokens)

    # 優先判定明確的缺貨狀態
    if has_out and not has_in:
        return 0
    if has_in and not has_out:
        return 1
    if has_out and has_in:
        # 同時出現時，以文字核心狀態為準
        if any(t in combined for t in ["在庫なし", "完売", "品切れ", "icon_stock_none", "stock_none"]):
            return 0
        if any(t in combined for t in ["在庫あり", "残りわずか", "icon_stock_ok", "stock_ok"]):
            return 1
        return 0

    return 0

def find_target_column_index(table, target_mi: str) -> int:
    thead = table.find("thead")
    header_tr = thead.find("tr") if thead else table.find("tr")
    if not header_tr:
        return -1

    headers = header_tr.find_all(["th", "td"])
    sku_headers = headers[1:] if len(headers) > 1 else headers

    for idx, th in enumerate(sku_headers):
        th_str = str(th)
        if target_mi in th_str:
            return idx
        th_classes = " ".join(th.get("class", []))
        if any(c in th_classes for c in ["selected", "active", "is-active", "current"]):
            return idx

    return -1

def parse_html_for_stores(html_content: str, target_mi: str) -> Dict[str, int]:
    results = {cfg["key"]: 0 for cfg in STORE_CONFIG}
    soup = BeautifulSoup(html_content, "html.parser")

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

        # 策略 1: 表格精確解析
        for table in tables:
            store_node = table.find(string=pattern)
            if not store_node:
                continue

            row = store_node.find_parent("tr")
            if not row:
                continue

            # 抓取該列所有單元格（包含 th 與 td）
            all_cells = row.find_all(["th", "td"])
            if not all_cells:
                continue

            # 找出店名所在的 cell index
            store_cell_idx = -1
            for i, cell in enumerate(all_cells):
                if cell.find(string=pattern) or pattern.search(cell.get_text()):
                    store_cell_idx = i
                    break

            # 排除店名單元格，取得真正的庫存單元格
            stock_cells = [cell for i, cell in enumerate(all_cells) if i != store_cell_idx]
            if not stock_cells:
                continue

            if len(stock_cells) == 1:
                # 單一規格（或頁面已按 mi 篩選）：唯一的 stock cell 即為目標狀態
                found_status = determine_stock(stock_cells[0])
                break
            else:
                # 多規格矩陣：尋找對應 SKU 欄位
                target_col_idx = find_target_column_index(table, target_mi)
                if 0 <= target_col_idx < len(stock_cells):
                    found_status = determine_stock(stock_cells[target_col_idx])
                    break
                else:
                    # 表頭無明確標記時，檢查個別 cell 是否有 mi 或 selected
                    for cell in stock_cells:
                        cell_str = str(cell)
                        cell_classes = " ".join(cell.get("class", []))
                        if target_mi in cell_str or any(c in cell_classes for c in ["selected", "active", "is-active"]):
                            found_status = determine_stock(cell)
                            break
                    if found_status is not None:
                        break

        # 策略 2: 結構化列表容器備援（如 dl / li 結構）
        if found_status is None:
            text_node = search_root.find(string=pattern)
            if text_node:
                curr = text_node.parent
                container = None
                while curr and curr.name not in ["body", "html"]:
                    if curr.name in ["tr", "li"]:
                        container = curr
                        break
                    # 若為 dl 或 div，僅限單一店家的外層容器
                    if curr.name in ["div", "dl"] and any(k in " ".join(curr.get("class", [])) for k in ["store", "shop", "item", "row"]):
                        container = curr
                        break
                    curr = curr.parent

                if container:
                    found_status = determine_stock(container)

        # 策略 3: 滑動視窗（純文字比對）
        if found_status is None:
            match = pattern.search(html_content)
            if match:
                pos = match.start()
                window_html = html_content[pos: min(len(html_content), pos + 350)]
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

        stock_results = parse_html_for_stores(resp.text, target_mi=mi)
        return {"status": "success", "mi": mi, "data": stock_results}

    except httpx.RequestError as e:
        return {"status": "error", "mi": mi, "message": f"Network error: {str(e)}", "data": {c["key"]: 0 for c in STORE_CONFIG}}
    except Exception as e:
        return {"status": "error", "mi": mi, "message": str(e), "data": {c["key"]: 0 for c in STORE_CONFIG}}
