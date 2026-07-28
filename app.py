from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import google.generativeai as genai
from PIL import Image
import io

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

GEMINI_API_KEY = "본인의_GEMINI_API_KEY_입력"
genai.configure(api_key=GEMINI_API_KEY)
vision_model = genai.GenerativeModel('gemini-1.5-flash')

class AnalyzeRequest(BaseModel):
    text: str

@app.post("/ocr")
async def process_ocr(file: UploadFile = File(...)):
    try:
        image_bytes = await file.read()
        image = Image.open(io.BytesIO(image_bytes))
        response = vision_model.generate_content([
            "Extract all the text from this aircraft load report image exactly as it appears. Maintain the line breaks and spaces.",
            image
        ])
        return {"text": response.text}
    except Exception as e:
        return {"error": str(e)}

def parse_report_data(text):
    data = {"lines": {}}
    fleet_type = "CEO"
    trigger_code = "UNKNOWN"
    gw_lbs = 0

    if "NEO" in text or "LEAP" in text or "PW11" in text or "A321-25" in text or "A321-27" in text:
        fleet_type = "NEO"

    lines = text.strip().split('\n')
    for line in lines:
        parts = line.strip().split()
        if not parts:
            continue
        
        header = parts[0]
        data["lines"][header] = parts[1:]

        if header == 'C1' and len(parts) >= 4:
            trigger_code = parts[3]

        if header == 'CE' and len(parts) >= 2:
            try:
                gw_lbs = int(parts[1]) * 100
            except ValueError:
                pass

    return fleet_type, trigger_code, data["lines"], gw_lbs

@app.post("/analyze")
def analyze_report(req: AnalyzeRequest):
    try:
        fleet_type, trigger_code, lines_dict, gw_lbs = parse_report_data(req.text)
        
        status = "GREEN"
        reason = "데이터 분석 완료"
        kpi_data = {}
        
        CEO_MLW = 166448
        NEO_MLW = 174606

        if fleet_type == "CEO" and trigger_code.startswith("4"):
            s3_vrta = 0.0
            t3_vrta = 0.0
            
            if "S3" in lines_dict and len(lines_dict["S3"]) >= 1:
                try: s3_vrta = float(lines_dict["S3"][0])
                except ValueError: pass
            if "T3" in lines_dict and len(lines_dict["T3"]) >= 1:
                try: t3_vrta = float(lines_dict["T3"][0])
                except ValueError: pass
            
            max_vrta = max(s3_vrta, t3_vrta)
            kpi_data["S3"] = {"VRTA": max_vrta}
            
            is_overweight = gw_lbs > CEO_MLW
            
            if not is_overweight:
                if max_vrta >= 2.86:
                    status = "RED"
                    reason = f"Severe Hard Landing\n- 기준: Normal Weight [GW({gw_lbs:,} lbs) <= MLW({CEO_MLW:,} lbs)]\n- VRTA: {max_vrta}g\n- LIMIT: >= 2.86g"
                elif max_vrta >= 2.6:
                    status = "AMBER"
                    reason = f"Hard Landing\n- 기준: Normal Weight [GW({gw_lbs:,} lbs) <= MLW({CEO_MLW:,} lbs)]\n- VRTA: {max_vrta}g\n- LIMIT: >= 2.6g"
                else:
                    status = "GREEN"
                    reason = f"Normal Landing (Limit Not Exceeded)\n- 기준: Normal Weight [GW({gw_lbs:,} lbs) <= MLW({CEO_MLW:,} lbs)]\n- VRTA: {max_vrta}g\n- LIMIT: < 2.6g"
            else:
                if max_vrta >= 2.6:
                    status = "RED"
                    reason = f"Severe Hard Overweight Landing\n- 기준: Overweight [GW({gw_lbs:,} lbs) > MLW({CEO_MLW:,} lbs)]\n- VRTA: {max_vrta}g\n- LIMIT: >= 2.6g"
                elif max_vrta >= 1.7:
                    status = "AMBER"
                    reason = f"Hard Overweight Landing\n- 기준: Overweight [GW({gw_lbs:,} lbs) > MLW({CEO_MLW:,} lbs)]\n- VRTA: {max_vrta}g\n- LIMIT: >= 1.7g"
                else:
                    status = "GREEN"
                    reason = f"Normal Landing (Limit Not Exceeded)\n- 기준: Overweight [GW({gw_lbs:,} lbs) > MLW({CEO_MLW:,} lbs)]\n- VRTA: {max_vrta}g\n- LIMIT: < 1.7g"
        
        elif fleet_type == "NEO" and trigger_code.startswith("4"):
            max_nz = 0.0
            max_ny = 0.0
            
            for key in ["U1", "U2"]:
                if key in lines_dict and len(lines_dict[key]) >= 2:
                    try:
                        nz = float(lines_dict[key][0])
                        ny = float(lines_dict[key][1])
                        max_nz = max(max_nz, nz)
                        max_ny = max(max_ny, ny)
                        kpi_data[key] = {"Nz_kpi": nz, "Ny_kpi": ny}
                    except ValueError:
                        pass
            
            if max_nz >= 2.06 or max_ny >= 0.5:
                status = "RED"
                reason = f"Severe Hard Landing\n- 측정치: Nz={max_nz}g, Ny={max_ny}g\n- LIMIT: Nz >= 2.06g or Ny >= 0.5g"
            elif max_nz >= 1.80 or max_ny >= 0.45:
                status = "AMBER"
                reason = f"Hard Landing\n- 측정치: Nz={max_nz}g, Ny={max_ny}g\n- LIMIT: Nz >= 1.80g or Ny >= 0.45g"
            else:
                status = "GREEN"
                reason = f"Normal Landing (Limit Not Exceeded)\n- 측정치: Nz={max_nz}g, Ny={max_ny}g\n- LIMIT 미달 (Nz < 1.80g and Ny < 0.45g)"
                
        elif trigger_code in ["5100", "5200", "5300"]:
            vrta = 0.0
            if "S3" in lines_dict and len(lines_dict["S3"]) >= 1:
                try: vrta = float(lines_dict["S3"][0])
                except ValueError: pass
            
            kpi_data["S3"] = {"VRTA": vrta}
            
            if trigger_code == "5300":
                if vrta <= 0.0 or vrta >= 2.0:
                    status = "RED"
                    reason = f"Inspection Required (Turbulence)\n- VRTA: {vrta}g\n- LIMIT: <= 0.0g or >= 2.0g"
                else:
                    status = "GREEN"
                    reason = f"No Inspection Required\n- VRTA: {vrta}g\n- LIMIT 초과 안함"
            else:
                if vrta <= -1.0 or vrta >= 2.5:
                    status = "RED"
                    reason = f"Inspection Required (Turbulence)\n- VRTA: {vrta}g\n- LIMIT: <= -1.0g or >= 2.5g"
                else:
                    status = "GREEN"
                    reason = f"No Inspection Required\n- VRTA: {vrta}g\n- LIMIT 초과 안함"

        elif trigger_code in ["5600", "5700"]:
            lata = 0.0
            if "S4" in lines_dict and len(lines_dict["S4"]) >= 1:
                try: lata = float(lines_dict["S4"][0])
                except ValueError: pass
                
            kpi_data["S4"] = {"LATA": lata}
            
            if lata > 0.41:
                status = "RED"
                reason = f"Severe High Lateral Acceleration\n- LATA: {lata}g\n- LIMIT: > 0.41g"
            elif lata >= 0.35:
                status = "AMBER"
                reason = f"High Lateral Acceleration\n- LATA: {lata}g\n- LIMIT: >= 0.35g"
            else:
                status = "GREEN"
                reason = f"Normal (Limit Not Exceeded)\n- LATA: {lata}g\n- LIMIT 미달 (< 0.35g)"
                
        else:
            reason = "정의되지 않은 코드이거나 판별 로직이 없습니다."

        mlw_lbs = CEO_MLW if fleet_type == "CEO" else NEO_MLW
        
        return {
            "fleet_type": fleet_type,
            "aircraft_id": lines_dict.get("AH", [""])[0],
            "trigger_code": trigger_code,
            "status": status,
            "reason": reason,
            "kpi_data": kpi_data,
            "gw_lbs": gw_lbs,
            "mlw_lbs": mlw_lbs
        }
        
    except Exception as e:
        return {"error": str(e)}

@app.get("/")
def read_root():
    import os
    from fastapi.responses import HTMLResponse
    html_path = os.path.join("templates", "index.html")
    if os.path.exists(html_path):
        with open(html_path, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read(), status_code=200)
    else:
        return HTMLResponse(content="<h1>index.html 파일을 찾을 수 없습니다.</h1>", status_code=404)
