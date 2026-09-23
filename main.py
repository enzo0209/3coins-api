import re
from contextlib import asynccontextmanager
from typing import Dict

from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
import httpx

STORE_CONFIG = [
    {"key": "+plus ゆめタウン福山店", "keyword": "ゆめタウン福山"},
    {"key": "+plus エミフルMASAKI店", "keyword": "エミフルMASAKI"},
    {"key": "+plus minamoa広島店", "keyword": "minamoa広島"},
    {"key": "+plus 広島本通店", "keyword": "広島本通"},
]

ALL_KEYWORDS = [c["keyword"] for c in STORE_CONFIG]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Referer": "https://www.palcloset.jp/",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}

# 共享 HTTP 客戶端 (連線池)
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

def extract_stock_status(element_soup_or_text) -> int:
    """
    從目標元素中提取文字與圖片 alt 屬性，判定是否有庫存：
    1 = 有庫存 (在庫あり / 残りわずか / ◎ / ○ / ▲)
    0 = 無庫存 (在庫なし / 完売 / × / 品切れ / 未知)
    """
    if hasattr(element_soup_or_text, "get_text"):
        # 合併純文字與 img 標籤的 alt 資訊
        text = element_soup_or_text.get_text(separator=" ", strip=True)
        img_alts = " ".join([img.get("alt", "") for img in element_soup_or_text.find_all("img") if img.get("alt")])
        combined = f"{text} {img_alts}"
    else:
        combined = str(element_soup_or_text)

    # 優先判定缺貨標記
    is_out = any(token in combined for token in ["在庫なし", "完売", "品切れ", "×", "販売終了"])
    is_in = any(token in combined for token in ["在庫あり", "残りわずか", "◎", "▲", "○"])

    if is_out:
        return 0
    if is_in:
        return 1
    return 0

def parse_html_for_stores(html_content: str) -> Dict[str, int]:
    results = {cfg["key"]: 0 for cfg in STORE_CONFIG}
    soup = BeautifulSoup(html_content, "html.parser")

    for cfg in STORE_CONFIG:
        store_key = cfg["key"]
        kw = cfg["keyword"]

        # 1. 鎖定包含關鍵字的最底層文字節點 (Text Node)
        text_node = soup.find(string=re.compile(re.escape(kw)))
        
        target_block = None
        if text_node:
            # 向上查找最靠近的列元素或區塊容器
            row_parent = text_node.find_parent(["tr", "li", "dl", "div"])
            if row_parent:
                row_text = row_parent.get_text(separator=" ", strip=True)
                # 確保區塊不包含其他目標店（防止誤選最外層大容器）
                other_kws = [k for k in ALL_KEYWORDS if k != kw]
                if not any(other in row_text for other in other_kws):
                    target_block = row_parent

        # 2. 備用方案：使用雙向滑動視窗 (店名前後各 350 字元)
        if target_block:
            results[store_key] = extract_stock_status(target_block)
        else:
            pos = html_content.find(kw)
            if pos != -1:
                start_pos = max(0, pos - 200)
                end_pos = min(len(html_content), pos + 400)
                window_html = html_content[start_pos:end_pos]
                plain = re.sub(r"<[^>]+>", " ", window_html)
                clean_text = re.sub(r"\s+", " ", plain).strip()
                results[store_key] = extract_stock_status(clean_text)

    return results

@app.get("/api/stock")
async def get_stock(mi: str = Query(..., description="PAL CLOSET 商品管理 ID (mi)")):
    url = f"https://www.palcloset.jp/addons/pal/store_stock/?mi={mi}&b=3coins"

    try:
        resp = await http_client.get(url)
        if resp.status_code != 200:
            return {"status": "error", "mi": mi, "data": {c["key"]: 0 for c in STORE_CONFIG}}

        stock_results = parse_html_for_stores(resp.text)
        return {"status": "success", "mi": mi, "data": stock_results}

    except httpx.RequestError as e:
        return {"status": "error", "mi": mi, "message": f"Network error: {str(e)}", "data": {c["key"]: 0 for c in STORE_CONFIG}}
    except Exception as e:
        return {"status": "error", "mi": mi, "message": str(e), "data": {c["key"]: 0 for c in STORE_CONFIG}}
