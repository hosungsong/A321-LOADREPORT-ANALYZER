import os
import json
import io
from PIL import Image
import google.generativeai as genai
from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"]
)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
if GEMINI_API_KEY: 
    genai.configure(api_key=GEMINI_API_KEY)

@app.get("/")
async def serve_frontend():
    if os.path.exists("templates/index.html"):
        return FileResponse("templates/index.html")
    elif os.path.exists("index.html"):
        return FileResponse("index.html")
    else:
        return HTMLResponse("<h1>화면 파일을 찾을 수 없습니다. 깃허브에 templates/index.html 파일이 있는지 확인해주세요.</h1>")

class AnalyzeRequest(BaseModel):
    text: str

CODE_DESC = {
    "4100": "Excessive Radio Altitude Rate (RALR)",
    "4310": "Over-weight red-bounce hard-landing",
    "4320": "Over-weight amber-bounce hard-landing",
    "4400": "Excessive Normal Acceleration (VRTA) (compared to the limit at landing) - during +/- 0.5 seconds before and after landing",
    "4410": "Over-weight red hard-landing",
    "4420": "Over-weight amber hard-landing",
    "4500": "Excessive Normal Acceleration (VRTA) (compared to the limit at landing with bounce) - during +/- 0.5 seconds at landing (VRTA > VRTAL1.2) or at bounce (VRTA > VRTAL1.3)",
    "4510": "Red bounce hard-landing",
    "4520": "Amber bounce hard-landing",
    "4610": "Red hard landing",
    "4620": "Amber hard landing",
    "4800": "Excessive Gross Weight (GW) (compared to Radio Altitude Rate (RALR)) - at dataset time at landing",
    "4900": "Excessive Gross Weight (GW) - compared to Normal Acceleration (VRTA) - during +/- 0.5 seconds before and after landing",
    "5100": "Excessive normal acceleration (VRTA), compared to the positive limit and flap in clean configuration",
    "5200": "Excessive normal acceleration (VRTA), compared to the negative limit and flap in clean configuration",
    "5300": "Excessive normal acceleration (VRTA), compared to the positive or negative limit with extended flaps",
    "5600": "Lateral acceleration (LATA) amber. If the value of the in-flight lateral acceleration is between 0.35 g and 0.41 g, the load report shows trigger code 5600.",
    "5700": "Lateral acceleration (LATA) red. If the value of the in-flight lateral acceleration is more than 0.41 g, the load report shows trigger code 5700."
}

def parse_s3_t3_value(v_str):
    v_str = v_str.replace('x', '').replace('n', '').strip()
    if not v_str: return 0.0
    try:
        return float(v_str) / 100.0
    except ValueError:
        return 0.0

@app.post("/analyze")
async def analyze_report(req: AnalyzeRequest):
    text = req.text
    lines = text.split('\n')
    
    fleet_type = "UNKNOWN"
    trigger_code = "UNKNOWN"
    aircraft_id = "UNKNOWN"
    kpi_data = {}
    gw_lbs = 0
    
    for line in lines:
        if "HL" in line:
            parts = line.split()
            for p in parts:
                if p.startswith("HL"):
                    aircraft_id = p
                    break
        elif "CODE:" in line:
            trigger_code = line.split("CODE:")[1].strip()
        elif line.startswith("C1 ") or line.startswith("C1\t"):
            parts = line.split()
            if len(parts) > 3: 
                trigger_code = parts[3] 
        elif line.startswith("CE ") or line.startswith("CE\t"):
            parts = line.split()
            if len(parts) >= 6:
                try:
                    gw_value = int(parts[5]) 
                    gw_lbs = gw_value * 100
                except: pass
        elif line.startswith("U1") or line.startswith("U2"):
            fleet_type = "NEO"
            parts = line.split()
            if len(parts) >= 4:
                try:
                    kpi_data[parts[0]] = {
                        "Nz_kpi": float(parts[1])/100, 
                        "Ny_kpi": float(parts[2])/100
                    }
                except: pass
        elif line.startswith("S3") or line.startswith("T3") or line.startswith("S4") or line.startswith("T4"):
            if fleet_type == "UNKNOWN": fleet_type = "CEO"
            parts = line.split()
            if len(parts) >= 4:
                kpi_data[parts[0]] = {
                    "VRTA": parse_s3_t3_value(parts[1]),
                    "LATA": parse_s3_t3_value(parts[3])
                }

    if fleet_type == "UNKNOWN" and trigger_code != "UNKNOWN":
        fleet_type = "CEO" 

    mlw_lbs = 166448 if fleet_type == "CEO" else 174606
    status = "UNKNOWN"
    reason = "판별 로직 오류"

    if trigger_code.startswith("4"):
        if fleet_type == "CEO":
            s3_vrta = kpi_data.get("S3", {}).get("VRTA", 0.0)
            t3_vrta = kpi_data.get("T3", {}).get("VRTA", 0.0)
            max_vrta = max(s3_vrta, t3_vrta)
            
            is_overweight = gw_lbs > mlw_lbs
            limit_amber = 1.7 if is_overweight else 2.6
            limit_red = 2.6 if is_overweight else 2.86
            weight_str = f"Overweight [GW({gw_lbs:,} lbs) > MLW({mlw_lbs:,} lbs)]" if is_overweight else f"Normal Weight [GW({gw_lbs:,} lbs) <= MLW({mlw_lbs:,} lbs)]"
            
            if max_vrta >= limit_red:
                status = "RED"
                reason = f"Severe Hard Landing\n- 판독값: VRTA {max_vrta}g (LIMIT: {limit_red}g 이상)\n- 기준: {weight_str}"
            elif max_vrta >= limit_amber:
                status = "AMBER"
                reason = f"Hard Landing\n- 판독값: VRTA {max_vrta}g (LIMIT: {limit_amber}g 이상)\n- 기준: {weight_str}"
            else:
                status = "GREEN"
                reason = f"Normal Landing (Limit Not Exceeded)\n- 판독값: VRTA {max_vrta}g (LIMIT: {limit_amber}g 미만)\n- 기준: {weight_str}"

        else: # NEO
            max_nz = max([v.get("Nz_kpi", 0) for v in kpi_data.values()]) if kpi_data else 0
            max_ny = max([v.get("Ny_kpi", 0) for v in kpi_data.values()]) if kpi_data else 0

            if max_nz >= 2.06 or max_ny >= 0.5:
                status, reason = "RED", f"Severe Hard Landing\n- 판독값: Nz {max_nz}g / Ny {max_ny}g\n- 기준: Nz >= 2.06 or Ny >= 0.5"
            elif max_nz >= 1.80 or max_ny >= 0.45:
                status, reason = "AMBER", f"Hard Landing\n- 판독값: Nz {max_nz}g / Ny {max_ny}g\n- 기준: Nz >= 1.80 or Ny >= 0.45"
            else:
                status, reason = "GREEN", f"Normal Landing (Limit Not Exceeded)\n- 판독값: Nz {max_nz}g / Ny {max_ny}g"

    elif trigger_code in ["5100", "5200", "5300"]:
        max_vrta = max([v.get("VRTA", 0) for v in kpi_data.values()]) if kpi_data else 0
        min_vrta = min([v.get("VRTA", 0) for v in kpi_data.values()]) if kpi_data else 0
        
        if trigger_code in ["5100", "5200"]:
            if max_vrta >= 2.5 or min_vrta <= -1.0:
                status, reason = "RED", f"Inspection Required: Turbulence/Maneuver\n- 판독값: MAX {max_vrta}g / MIN {min_vrta}g\n- 기준: VRTA >= 2.5g or VRTA <= -1.0g"
            else:
                status, reason = "GREEN", f"No Inspection Required (Limit Not Exceeded)\n- 판독값: MAX {max_vrta}g / MIN {min_vrta}g"
        elif trigger_code == "5300":
            if max_vrta >= 2.0 or min_vrta <= 0.0:
                 status, reason = "RED", f"Inspection Required: Turbulence/Maneuver (Flaps Ext)\n- 판독값: MAX {max_vrta}g / MIN {min_vrta}g\n- 기준: VRTA >= 2.0g or VRTA <= 0.0g"
            else:
                 status, reason = "GREEN", f"No Inspection Required (Limit Not Exceeded)\n- 판독값: MAX {max_vrta}g / MIN {min_vrta}g"

    elif trigger_code in ["5600", "5700"]:
        max_lata = max([v.get("LATA", 0) for v in kpi_data.values()]) if kpi_data else 0
        if max_lata > 0.41:
            status, reason = "RED", f"Severe High Lateral\n- 판독값: LATA {max_lata}g (LIMIT: > 0.41g)"
        elif max_lata >= 0.35:
            status, reason = "AMBER", f"High Lateral\n- 판독값: LATA {max_lata}g (LIMIT: >= 0.35g)"
        else:
            status, reason = "GREEN", f"No Inspection Required (Limit Not Exceeded)\n- 판독값: LATA {max_lata}g"
    else:
        status = "UNKNOWN"
        reason = f"분석 불가 (코드 매칭 실패)"

    trigger_desc = CODE_DESC.get(trigger_code, "상세 설명이 등록되지 않은 코드입니다.")

    return {
        "status": status,
        "fleet_type": fleet_type,
        "trigger_code": trigger_code,
        "trigger_desc": trigger_desc,
        "aircraft_id": aircraft_id,
        "kpi_data": kpi_data,
        "reason": reason,
        "gw_lbs": gw_lbs,
        "mlw_lbs": mlw_lbs
    }

@app.post("/ocr")
async def extract_text_from_image(file: UploadFile = File(...)):
    if not GEMINI_API_KEY: return {"error": "API Key 미설정"}
    try:
        content = await file.read()
        model = genai.GenerativeModel('gemini-flash-lite-latest') 
        image_part = {"mime_type": file.content_type or "image/jpeg", "data": content}
        prompt = "이 이미지는 항공기 정비용 Load Report 영수증입니다. 인쇄된 모든 텍스트를 줄바꿈을 유지하여 정확하게 추출해주세요. 다른 설명 없이 텍스트만 반환하세요."
        response = await model.generate_content_async([prompt, image_part])
        return {"text": response.text.strip()}
    except Exception as e:
        return {"error": f"AI OCR 오류: {str(e)}"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
