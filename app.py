import os, json, re, smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import google.generativeai as genai
from fastapi import FastAPI, UploadFile, File, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import List
import io
from PIL import Image

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
if GEMINI_API_KEY: 
    genai.configure(api_key=GEMINI_API_KEY)

APP_DB = {"flights": [], "ataDatabase": [], "actionDatabase": [], "ac": {}, "emails": {}}
LEARNING_FILE = "learning_dict.json"

# A321 하드랜딩을 위한 기번 DB
FLEET_DB = {
    "HL8398": "NEO", "HL8364": "NEO", "HL8356": "NEO", "HL8399": "NEO",
    "HL8371": "NEO", "HL8395": "NEO", "HL8510": "NEO", "HL8515": "NEO",
}

# 💡 하드랜딩 분석 로직 (AMM 매뉴얼 기반)
def parse_kpi_value(val_str):
    try: return int(val_str) / 100.0
    except ValueError: return 0.0

def evaluate_landing_severity(nz, ny):
    if nz >= 2.06 or ny >= 0.50: return "RED", "Severe Hard Landing"
    elif 1.80 <= nz < 2.06 or 0.45 <= ny < 0.50: return "AMBER", "Hard Landing"
    else: return "GREEN", "Normal Landing (Limit Not Exceeded)"

def evaluate_in_flight_severity(lata):
    if lata > 0.41: return "RED", "Severe High Lateral Acceleration"
    elif 0.35 <= lata <= 0.41: return "AMBER", "High Lateral Acceleration"
    else: return "GREEN", "Spurious Report (Limit Not Exceeded)"

class AnalyzeRequest(BaseModel):
    text: str

# --- 기존 DB 로드 관련 함수 유지 ---
def load_learning_dict():
    if os.path.exists(LEARNING_FILE):
        with open(LEARNING_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_learning_dict(data):
    with open(LEARNING_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def apply_learning(text, l_dict):
    if not text: return text
    for wrong, right in l_dict.items():
        if not wrong: continue
        pattern = re.compile(re.escape(wrong), re.IGNORECASE)
        text = pattern.sub(right, text)
    return text

def reload_db_from_lines(lines):
    APP_DB["flights"].clear()
    APP_DB["ataDatabase"].clear()
    APP_DB["actionDatabase"].clear()
    APP_DB["ac"].clear()
    APP_DB["emails"].clear()
    
    for idx, line in enumerate(lines):
        rowNum = idx + 1
        parts = [p.strip() for p in line.split(',')]
        if len(parts) >= 2:
            type_ = parts[0].upper()
            if type_ == 'ATA':
                key = ",".join(parts[2:]).upper() if len(parts) > 2 else ""
                if key and parts[1] and key != 'KEYWORD':
                    APP_DB["ataDatabase"].append({"keyword": key, "code": parts[1], "row": rowNum})
            elif type_ == 'NEF' and len(parts) >= 3:
                key = ",".join(parts[2:]).upper()
                APP_DB["actionDatabase"].append({"type": 'NEF', "code": parts[1].upper(), "acType": 'ALL', "keyword": key, "row": rowNum})
            elif type_ == 'MEL' and len(parts) >= 4:
                key = ",".join(parts[3:]).upper()
                APP_DB["actionDatabase"].append({"type": 'MEL', "acType": parts[1].upper(), "code": parts[2].upper(), "keyword": key, "row": rowNum})
            elif type_ == 'ACTION' and len(parts) >= 3:
                key = ",".join(parts[2:]).upper()
                if key and parts[1] and key != 'KEYWORD':
                    APP_DB["actionDatabase"].append({"type": '', "code": parts[1].upper(), "acType": 'ALL', "keyword": key, "row": rowNum})
            elif type_ == 'FLIGHT' and len(parts) >= 4:
                APP_DB["flights"].append({"no": parts[1], "from": parts[2].upper(), "to": parts[3].upper()})
            elif type_ == 'AC' and len(parts) >= 3:
                APP_DB["ac"][parts[1]] = parts[2]
            elif type_ == 'EMAIL' and len(parts) >= 3:
                APP_DB["emails"][parts[1].upper()] = ",".join(parts[2:]).strip()

@app.on_event("startup")
def startup_event():
    if os.path.exists("database.csv"):
        with open("database.csv", "r", encoding="utf-8-sig") as f:
            reload_db_from_lines(f.readlines())

@app.get("/")
async def serve_frontend(): 
    if os.path.exists("templates/index.html"):
        return FileResponse("templates/index.html")
    return FileResponse("index.html")

@app.get("/ping")
@app.head("/ping")
@app.head("/")
async def keep_alive_ping(): return {"status": "awake"}

# 💡 A321 하드랜딩 리포트 분석 엔드포인트
@app.post("/analyze")
async def analyze_data(req: AnalyzeRequest):
    report_text = req.text
    result = {
        "aircraft_id": "Unknown", "fleet_type": "Unknown",
        "trigger_code": "Unknown", "status": "UNKNOWN", "reason": "분석 불가",
        "kpi_data": {}
    }

    # 기번 추출
    ac_match = re.search(r'(HL\d{4})', report_text)
    if ac_match:
        result["aircraft_id"] = ac_match.group(1)
        result["fleet_type"] = FLEET_DB.get(result["aircraft_id"], "CEO")

    # 코드 추출
    code_match = re.search(r'CODE.*?(\d{4})', report_text)
    if code_match: result["trigger_code"] = code_match.group(1)

    code = result["trigger_code"]

    # Landing 파싱
    if code.startswith('4'):
        u1_match = re.search(r'U1\s+(-?\d{3})\s+(-?\d{3})', report_text)
        u2_match = re.search(r'U2\s+(-?\d{3})\s+(-?\d{3})', report_text)
        
        nz_kpi1, ny_kpi1, nz_kpi2, ny_kpi2 = 0.0, 0.0, 0.0, 0.0
        if u1_match:
            nz_kpi1 = parse_kpi_value(u1_match.group(1))
            ny_kpi1 = parse_kpi_value(u1_match.group(2))
            result["kpi_data"]["U1"] = {"Nz_kpi": nz_kpi1, "Ny_kpi": ny_kpi1}
            
        if u2_match:
            nz_kpi2 = parse_kpi_value(u2_match.group(1))
            ny_kpi2 = parse_kpi_value(u2_match.group(2))
            result["kpi_data"]["U2"] = {"Nz_kpi": nz_kpi2, "Ny_kpi": ny_kpi2}

        max_nz = max(nz_kpi1, nz_kpi2)
        max_ny = max(ny_kpi1, ny_kpi2)
        
        status, reason = evaluate_landing_severity(max_nz, max_ny)
        
        # Spurious 판별 로직 추가
        if status == "GREEN" and code in ["4510", "4520", "4610", "4620"]:
            status = "GREEN (SPURIOUS)"
            reason = "코드는 AMBER/RED이나 수치가 LIMIT 이내입니다 (Spurious 가능성)"

        result["status"] = status
        result["reason"] = reason

    # Turbulence 파싱
    elif code.startswith('5'):
        e1_match = re.search(r'E1\s+(-?\d{3}|-?\d{4})', report_text)
        if e1_match:
            lata_val = parse_kpi_value(e1_match.group(1))
            result["kpi_data"]["E1"] = {"LATA": lata_val}
            status, reason = evaluate_in_flight_severity(abs(lata_val))
            result["status"] = status
            result["reason"] = reason

    return result

# 💡 OCR 처리 (기존 A321 이미지 분석용으로 통합)
@app.post("/ocr")
async def extract_text(file: UploadFile = File(...)):
    if not GEMINI_API_KEY: return {"error": "API Key 미설정"}
    try:
        content = await file.read()
        model = genai.GenerativeModel('gemini-flash-lite-latest') 
        
        # 회전 보정
        try:
            img = Image.open(io.BytesIO(content))
            if img.height > img.width:
                orient_prompt = "이 이미지는 항공 정비 로그의 일부야. 글자들이 수평으로 똑바로 서 보이기 위해 이미지를 시계 방향으로 몇 도 돌려야 할까? (0, 90, 180, 270 중 숫자 하나만 대답해)"
                res_orient = await model.generate_content_async([orient_prompt, {"mime_type": file.content_type or "image/jpeg", "data": content}])
                deg_str = re.sub(r'[^0-9]', '', res_orient.text.strip())
                if deg_str in ["90", "180", "270"]:
                    img = img.rotate(-int(deg_str), expand=True)
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG")
                    content = buf.getvalue()
        except Exception: pass

        image_part = {"mime_type": file.content_type or "image/jpeg", "data": content}
        
        # A321 Load Report 전용 프롬프트로 변경
        prompt = """
        당신은 항공기 Load Report <15> 분석가입니다.
        이미지에 보이는 텍스트를 줄바꿈이나 띄어쓰기를 최대한 원본 그대로 유지하여 전부 텍스트로 추출하세요.
        특히 'HL'로 시작하는 기번, 'CODE' 번호, 'U1', 'U2', 'E1' 뒤에 있는 숫자들은 분석에 매우 중요하므로 절대 틀리지 않게 정확히 추출하세요.
        """
        response = await model.generate_content_async([prompt, image_part])
        return {"text": response.text.strip()}
    except Exception as e: return {"error": f"AI 분석 오류: {str(e)}"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
