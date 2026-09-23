import asyncio
import re
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
import httpx
from bs4 import BeautifulSoup

app = FastAPI(title="3COINS Stock Proxy")

# 允許跨域連線 (CORS)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 定義目標分店與精準比對關鍵字
STORE_CONFIG = [
    {"key": "+plus ゆめタウン福山店", "keyword": "ゆめタウン福山"},
    {"key": "+plus エミフルMASAKI店", "keyword": "エミフルMASAKI"},
    {"key": "+plus minamoa広島店", "keyword": "minamoa広島"},
    {"key": "+plus 広島本通店", "keyword": "広島本通"}
]

ALL_KEYWORDS = [c["keyword"] for c in STORE_CONFIG]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Referer": "https://www.palcloset.jp/",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8"
}

def extract_stock_from_isolated_text(text: str) -> int:
    """從隔離的單店區塊文字中精準解析庫存狀態"""
    # 優先判斷明確缺貨標記 (避免免責聲明中的字詞干擾)
    is_out = ("在庫なし" in text) or ("完売" in text) or ("×" in text) or ("品切れ" in text)
    is_in = ("在庫あり" in text) or ("残りわずか" in text) or ("◎" in text) or ("▲" in text) or ("○" in text)

    # 若明確標記為缺貨，優先判定為 0
    if is_out and not ("在庫あり" in text and "在庫なし" not in text):
        return 0
    elif is_in:
        return 1
    return 0

def parse_html_for_stores(html_content: str) -> dict:
    results = {}
    soup = BeautifulSoup(html_content, "html.parser")
    
    for cfg in STORE_CONFIG:
        store_key = cfg["key"]
        kw = cfg["keyword"]
        results[store_key] = 0
        
        found_block_text = None
        
        # 1. 優先使用 BeautifulSoup 尋找包含該店關鍵字的 DOM 節點
        target_node = soup.find(lambda e: e.name in ['td', 'th', 'dt', 'span', 'p', 'div', 'a'] and kw in e.get_text())
        if target_node:
            # 向上找到專屬於該店的列元素 (tr 或 li)
            row_parent = target_node.find_parent(['tr', 'li'])
            if row_parent:
                row_text = row_parent.get_text(separator=' ', strip=True)
                # 確保該 row 沒有跨到其他目標分店 (確認為單店獨立行)
                other_kws = [k for k in ALL_KEYWORDS if k != kw]
                if not any(ok in row_text for ok in other_kws):
                    found_block_text = row_text

        # 2. 若 DOM 結構較為特殊，使用嚴格邊界保護 (截取範圍不可超過下一家分店)
        if not found_block_text:
            pos = html_content.find(kw)
            if pos != -1:
                next_boundary = pos + 150
                for ok in ALL_KEYWORDS:
                    if ok != kw:
                        next_pos = html_content.find(ok, pos + len(kw))
                        if next_pos != -1 and next_pos < next_boundary:
                            next_boundary = next_pos
                
                sub_html = html_content[pos:next_boundary]
                plain = re.sub(r'<[^>]+>', ' ', sub_html)
                found_block_text = re.sub(r'\s+', ' ', plain).strip()

        if found_block_text:
            results[store_key] = extract_stock_from_isolated_text(found_block_text)

    return results

@app.get("/api/stock")
async def get_stock(mi: str = Query(...)):
    url = f"https://www.palcloset.jp/addons/pal/store_stock/?mi={mi}&b=3coins"
    
    async with httpx.AsyncClient(headers=HEADERS, timeout=15.0, follow_redirects=True) as client:
        try:
            resp = await client.get(url)
            if resp.status_code != 200:
                return {"status": "error", "mi": mi, "data": {c["key"]: 0 for c in STORE_CONFIG}}

            stock_results = parse_html_for_stores(resp.text)
            return {"status": "success", "mi": mi, "data": stock_results}

        except Exception as e:
            return {"status": "error", "mi": mi, "message": str(e), "data": {c["key"]: 0 for c in STORE_CONFIG}}
