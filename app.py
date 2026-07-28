import os, json, re
import google.generativeai as genai
from fastapi import FastAPI, UploadFile, File, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
import io
from PIL import Image

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
if GEMINI_API_KEY: genai.configure(api_key=GEMINI_API_KEY)

FLEET_DB = {
    "HL8071": "CEO", "HL8256": "CEO", "HL8257": "CEO", "HL8267": "CEO", 
    "HL8364": "NEO", "HL8371": "NEO", "HL8398": "NEO", "HL8399": "NEO", 
    "HL8356": "NEO", "HL8395": "NEO", "HL8510": "NEO", "HL8515": "NEO"
}

# 기종별 MLW (LBS)
MLW_DB = {
    "CEO": 166448,
    "NEO": 174606
}

def parse_kpi_value(val_str):
    try: return abs(float(val_str)) / 100.0
    except Exception: return 0.0

def evaluate_severity(code, fleet_type, max_nz, max_ny, max_e1, gw_lbs, mlw_lbs):
    status = "UNKNOWN"
    reason_text = "데이터를 분석할 수 없습니다."
    
    if code.startswith('4'):
        if fleet_type == "CEO":
            # 💡 CEO: GW와 MLW 직접 비교 (사용자 요청 로직)
            is_overweight = gw_lbs > mlw_lbs
            weight_status = f"GW({gw_lbs:,} lbs) > MLW({mlw_lbs:,} lbs)" if is_overweight else f"GW({gw_lbs:,} lbs) <= MLW({mlw_lbs:,} lbs)"
            
            if is_overweight:
                if max_nz >= 2.6: status, reason_text = "RED", f"Severe Hard Overweight Landing [{weight_status}]"
                elif max_nz >= 1.7: status, reason_text = "AMBER", f"Hard Overweight Landing [{weight_status}]"
                else: status, reason_text = "GREEN", f"Normal Landing (Limit Not Exceeded) [{weight_status}]"
            else:
                if max_nz >= 2.86: status, reason_text = "RED", f"Severe Hard Landing [{weight_status}]"
                elif max_nz >= 2.6: status, reason_text = "AMBER", f"Hard Landing [{weight_status}]"
                else: status, reason_text = "GREEN", f"Normal Landing (Limit Not Exceeded) [{weight_status}]"
        else:
            # 💡 NEO: GW 무관하게 절대 수치 비교 (2.06, 1.80)
            if max_nz >= 2.06 or max_ny >= 0.50: status, reason_text = "RED", "Severe Hard Landing (NEO)"
            elif max_nz >= 1.80 or max_ny >= 0.45: status, reason_text = "AMBER", "Hard Landing (NEO)"
            else: status, reason_text = "GREEN", "Normal Landing (Limit Not Exceeded)"
            
    elif code.startswith('5'):
        if code in ['5600', '5700']:
            if max_e1 > 0.41: status, reason_text = "RED", "High Lateral Acceleration (LATA > 0.41g)"
            elif max_e1 >= 0.35: status, reason_text = "AMBER", "High Lateral Acceleration (0.35g <= LATA <= 0.41g)"
            else: status, reason_text = "GREEN", "Normal Lateral Accel (Limit Not Exceeded)"
        else:
            status, reason_text = "AMBER", f"Excessive Turbulence (VRTA). AMM Limit Check Required (Code: {code})"
            
    return status, reason_text

@app.post("/ocr")
async def extract_text_from_image(file: UploadFile = File(...)):
    if not GEMINI_API_KEY: return {"error": "API Key 미설정"}
    try:
        content = await file.read()
        image_part = {"mime_type": file.content_type or "image/jpeg", "data": content}
        model = genai.GenerativeModel('gemini-flash-lite-latest') 
        prompt = "이미지에 있는 텍스트를 그대로 모두 추출해줘. Load Report의 포맷을 절대 망치지 말고 띄어쓰기를 유지해."
        response = await model.generate_content_async([prompt, image_part])
        return {"text": response.text.strip()}
    except Exception as e: return {"error": f"AI 분석 오류: {str(e)}"}

@app.post("/analyze")
async def analyze_report(payload: dict = Body(...)):
    text = payload.get("text", "")
    if not text: return {"error": "입력된 데이터가 없습니다."}

    result = {
        "fleet_type": "NEO", "aircraft_id": "UNKNOWN", 
        "trigger_code": "UNKNOWN", "status": "UNKNOWN", "reason": "", 
        "gw_lbs": 0, "mlw_lbs": MLW_DB["NEO"], "kpi_data": {}
    }

    # 1. 기번 및 기종 판별
    ac_match = re.search(r'(HL\d{4})', text)
    if ac_match:
        result["aircraft_id"] = ac_match.group(1)
        result["fleet_type"] = FLEET_DB.get(result["aircraft_id"], "NEO")
        result["mlw_lbs"] = MLW_DB[result["fleet_type"]]

    # 2. 코드 판별
    code_match = re.search(r'\b(43\d{2}|44\d{2}|45\d{2}|46\d{2}|48\d{2}|49\d{2}|51\d{2}|52\d{2}|53\d{2}|56\d{2}|57\d{2})\b', text)
    if code_match: result["trigger_code"] = code_match.group(1)

    # 3. GW (Gross Weight) 파싱
    # CE 0325 00028 139 210 1570 225 I62R02 형태에서 5번째 숫자 추출
    ce_match = re.search(r'CE\s+\S+\s+\S+\s+\S+\s+\S+\s+(\d+)', text)
    if ce_match:
        result["gw_lbs"] = int(ce_match.group(1)) * 100

    max_nz, max_ny, max_e1 = 0.0, 0.0, 0.0

    # 4. 데이터 파싱
    if result["fleet_type"] == "CEO" and result["trigger_code"].startswith('4'):
        # CEO Hardlanding (S3, T3 파싱)
        s3_match = re.search(r'S3\s+([x\d]+)\s+([-0]?\d+)\s+([-0]?\d+)', text, re.IGNORECASE)
        if s3_match and s3_match.group(1).lower() != 'xnnn':
            max_nz = parse_kpi_value(s3_match.group(1))
            max_ny = parse_kpi_value(s3_match.group(3))
            result["kpi_data"]["S3 (First Touch)"] = {"Nz_kpi": max_nz, "Ny_kpi": max_ny}

        t3_match = re.search(r'T3\s+([x\d]+)\s+([-0]?\d+)\s+([-0]?\d+)', text, re.IGNORECASE)
        if t3_match and t3_match.group(1).lower() != 'xnnn':
            nz2 = parse_kpi_value(t3_match.group(1))
            ny2 = parse_kpi_value(t3_match.group(3))
            max_nz, max_ny = max(max_nz, nz2), max(max_ny, ny2)
            result["kpi_data"]["T3 (Bounce)"] = {"Nz_kpi": nz2, "Ny_kpi": ny2}

    elif result["fleet_type"] == "NEO" and result["trigger_code"].startswith('4'):
        # NEO Hardlanding (U1, U2 파싱)
        u1_match = re.search(r'U1\s+([-0]?\d+)\s+([-0]?\d+)', text)
        if u1_match:
            max_nz = parse_kpi_value(u1_match.group(1))
            max_ny = parse_kpi_value(u1_match.group(2))
            result["kpi_data"]["U1 (First Touch)"] = {"Nz_kpi": max_nz, "Ny_kpi": max_ny}

        u2_match = re.search(r'U2\s+([-0]?\d+)\s+([-0]?\d+)', text)
        if u2_match:
            nz2, ny2 = parse_kpi_value(u2_match.group(1)), parse_kpi_value(u2_match.group(2))
            max_nz, max_ny = max(max_nz, nz2), max(max_ny, ny2)
            result["kpi_data"]["U2 (Bounce)"] = {"Nz_kpi": nz2, "Ny_kpi": ny2}

    if result["trigger_code"].startswith('5'):
        e1_match = re.search(r'E1\s+([-0]?\d+)', text)
        if e1_match: 
            max_e1 = parse_kpi_value(e1_match.group(1))
            result["kpi_data"]["E1 (MAX)"] = {"LATA/VRTA": max_e1}
            max_ny = max_e1

    # 5. 최종 판별
    result["status"], result["reason"] = evaluate_severity(
        result["trigger_code"], result["fleet_type"], max_nz, max_ny, max_e1, result["gw_lbs"], result["mlw_lbs"]
    )

    # 6. Spurious 경고 추가 (Delta 기준 명시)
    if "GREEN" in result["status"] and result["trigger_code"] != "UNKNOWN":
        result["reason"] += "\n💡 [Spurious Report 의심됨]: Report Code는 조치를 요구하나 실제 파싱된 값(Max)은 Green Zone입니다."
        if result["trigger_code"] in ['5600', '5700']:
            result["reason"] += "\n(🚨 주의: LATA Spurious 판별 시 단순 0g 기준이 아닌, 주변 평균값 대비 Delta(Max-Min) 0.09g 초과 여부 확인 필수)"

    return result

@app.get("/")
async def serve_frontend(): 
    return FileResponse("templates/index.html")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
