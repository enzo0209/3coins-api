import asyncio
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
import httpx
from bs4 import BeautifulSoup

app = FastAPI(title="3COINS Stock Proxy")

# 允許跨域連線 (CORS)，讓前端 PWA 順利取得資料
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

TARGET_STORES = [
    "+plus ゆめタウン福山店",
    "+plus エミフルMASAKI店",
    "+plus minamoa広島店",
    "+plus 広島本通店"
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Referer": "https://www.palcloset.jp/",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8"
}

@app.get("/api/stock")
async def get_stock(mi: str = Query(...)):
    url = f"https://www.palcloset.jp/addons/pal/store_stock/?mi={mi}&b=3coins"
    
    async with httpx.AsyncClient(headers=HEADERS, timeout=15.0, follow_redirects=True) as client:
        try:
            resp = await client.get(url)
            if resp.status_code != 200:
                return {"status": "error", "mi": mi, "data": {s: 0 for s in TARGET_STORES}}

            soup = BeautifulSoup(resp.text, "html.parser")
            text_content = soup.get_text()

            # 解析四家分店庫存
            results = {}
            for store in TARGET_STORES:
                # 截取店名周圍 250 個字元判斷庫存狀態標記
                if store in text_content:
                    idx = text_content.find(store)
                    snippet = text_content[idx:idx + 250]
                    if "在庫あり" in snippet or "残りわずか" in snippet:
                        results[store] = 1
                    else:
                        results[store] = 0
                else:
                    results[store] = 0

            return {"status": "success", "mi": mi, "data": results}

        except Exception as e:
            return {"status": "error", "mi": mi, "message": str(e), "data": {s: 0 for s in TARGET_STORES}}