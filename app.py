import os, json, re
import google.generativeai as genai
from fastapi import FastAPI, UploadFile, File, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
import io
from PIL import Image

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# 💡 환경변수에서 Gemini API 키 가져오기 (Render 설정에서 넣어야 합니다)
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
if GEMINI_API_KEY: 
    genai.configure(api_key=GEMINI_API_KEY)

# 기번-기종 DB (향후 추가 가능)
FLEET_DB = {
    "HL8398": "NEO", "HL8364": "NEO", "HL8356": "NEO", "HL8399": "NEO",
    "HL8371": "NEO", "HL8395": "NEO", "HL8510": "NEO", "HL8515": "NEO",
}

# 💡 프론트엔드 HTML 서빙 (기존 작성하셨던 방식과 동일)
@app.get("/")
async def serve_frontend(): 
    # Flask와 달리 FastAPI의 FileResponse는 기본적으로 최상위 경로에서 찾습니다.
    # 깃허브에 올리실 때 index.html을 최상위(app.py와 같은 곳)에 두셔도 되고,
    # 아래처럼 templates 폴더 안에 두셨다면 경로를 지정해 줍니다.
    if os.path.exists("templates/index.html"):
        return FileResponse("templates/index.html")
    return FileResponse("index.html")

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

# 💡 텍스트 데이터 분석 엔드포인트
@app.post("/analyze")
async def analyze_data(req: AnalyzeRequest):
    report_text = req.text
    result = {
        "aircraft_id": "Unknown", "fleet_type": "Unknown",
        "trigger_code": "Unknown", "status": "UNKNOWN", "reason": "분석 불가",
        "kpi_data": {}
    }

    # 1. 기번 및 코드 추출
    ac_match = re.search(r'(HL\d{4})', report_text)
    if ac_match:
        result["aircraft_id"] = ac_match.group(1)
        result["fleet_type"] = FLEET_DB.get(result["aircraft_id"], "CEO")

    code_match = re.search(r'CODE.*?(\d{4})', report_text)
    if code_match: result["trigger_code"] = code_match.group(1)

    code = result["trigger_code"]

    # 2. 하드랜딩 로직 (Code 4XXX)
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

    # 3. Turbulence / LATA 로직 (Code 5XXX)
    elif code.startswith('5'):
        e1_match = re.search(r'E1\s+(-?\d{3}|-?\d{4})', report_text)
        if e1_match:
            lata_val = parse_kpi_value(e1_match.group(1))
            result["kpi_data"]["E1"] = {"LATA": lata_val}
            status, reason = evaluate_in_flight_severity(abs(lata_val))
            result["status"] = status
            result["reason"] = reason

    return result

# 💡 이미지 OCR 처리 엔드포인트 (기존 로직 차용)
@app.post("/ocr")
async def extract_text(file: UploadFile = File(...)):
    if not GEMINI_API_KEY: return {"error": "API Key 미설정"}
    try:
        content = await file.read()
        model = genai.GenerativeModel('gemini-flash-lite-latest') 
        
        # 기존 코드의 이미지 회전 보정 로직 (그대로 유지)
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
        
        # 하드랜딩 리포트 추출용 프롬프트
        prompt = """
        당신은 항공기 Load Report <15> 분석가입니다.
        이미지에 보이는 텍스트를 줄바꿈이나 띄어쓰기를 최대한 원본 그대로 유지하여 전부 텍스트로 추출하세요.
        특히 'HL'로 시작하는 기번, 'CODE' 번호, 'U1', 'U2', 'E1' 뒤에 있는 숫자들은 분석에 매우 중요하므로 절대 틀리지 않게 정확히 추출하세요.
        """
        response = await model.generate_content_async([prompt, image_part])
        return {"text": response.text.strip()}
    except Exception as e: 
        return {"error": f"AI 분석 오류: {str(e)}"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))        return "GREEN", "Normal Landing (Limit Not Exceeded)"

# 원리: 사용자가 복사/붙여넣기 한 텍스트 덩어리 속에서 필요한 키워드(U1, U2, CODE 등) 주변의 숫자만 정확히 뽑아냅니다.
def analyze_report_text(report_text):
    result = {
        "aircraft_id": "Unknown",
        "fleet_type": "Unknown",
        "trigger_code": "Unknown",
        "status": "UNKNOWN",
        "reason": "데이터를 분석할 수 없습니다.",
        "kpi_data": {}
    }

    # 1. 기번(A/C ID) 추출 (HL로 시작하는 4자리 숫자)
    ac_match = re.search(r'(HL\d{4})', report_text)
    if ac_match:
        result["aircraft_id"] = ac_match.group(1)
        # 딕셔너리에서 기종 찾기 (없으면 기본값 CEO로 설정 - 추후 수정 가능)
        result["fleet_type"] = FLEET_DB.get(result["aircraft_id"], "CEO")

    # 2. Trigger Code 추출 (예: 4520, 4610, 5700 등)
    code_match = re.search(r'CODE.*?(\d{4})', report_text)
    if code_match:
        result["trigger_code"] = code_match.group(1)

    # 3. U1 (First Touchdown) 값 추출
    # 정규식 패턴: 'U1' 다음에 나오는 첫번째 숫자 덩어리(DNZKPI), 두번째(NYKPI) 추출
    u1_match = re.search(r'U1\s+(-?\d{3})\s+(-?\d{3})', report_text)
    nz_kpi1, ny_kpi1 = 0.0, 0.0
    if u1_match:
        nz_kpi1 = parse_kpi_value(u1_match.group(1))
        ny_kpi1 = parse_kpi_value(u1_match.group(2))
        result["kpi_data"]["U1"] = {"Nz_kpi": nz_kpi1, "Ny_kpi": ny_kpi1}

    # 4. U2 (Second Touchdown / Bounce) 값 추출
    u2_match = re.search(r'U2\s+(-?\d{3})\s+(-?\d{3})', report_text)
    nz_kpi2, ny_kpi2 = 0.0, 0.0
    if u2_match:
        nz_kpi2 = parse_kpi_value(u2_match.group(1))
        ny_kpi2 = parse_kpi_value(u2_match.group(2))
        result["kpi_data"]["U2"] = {"Nz_kpi": nz_kpi2, "Ny_kpi": ny_kpi2}

    if result["trigger_code"].startswith('4'):
        # U1과 U2 중 더 심각한 값을 기준으로 평가합니다. (Max 값 추출)
        max_nz = max(nz_kpi1, nz_kpi2)
        max_ny = max(ny_kpi1, ny_kpi2)
        
    # 분석 결과를 JSON 형태로 프론트엔드에 반환합니다.
    return jsonify(analysis_result)

# 원리: 사용자가 웹 브라우저로 접속했을 때 첫 화면(index.html)을 보여주는 라우트입니다.
@app.route('/')
def home():
    return render_template('index.html')

if __name__ == '__main__':
    # 로컬 테스트를 위한 서버 실행 코드
    app.run(debug=True, port=5000)
