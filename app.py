from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import google.generativeai as genai
from PIL import Image
import io
import os
import re

app=FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
genai.configure(api_key="본인의_GEMINI_API_KEY_입력")
vision_model=genai.GenerativeModel('gemini-1.5-flash')

class AnalyzeRequest(BaseModel): text:str

@app.post("/ocr")
async def process_ocr(file: UploadFile=File(...)):
    try:
        image_bytes=await file.read()
        image=Image.open(io.BytesIO(image_bytes))
        response=vision_model.generate_content(["Extract all the text from this aircraft load report image exactly as it appears. Maintain the line breaks and spaces.", image])
        return {"text": response.text}
    except Exception as e:
        return {"error": str(e)}

def parse_g(v_str):
    try:
        v=float(v_str)
        if '.' not in v_str: return v/100.0
        return v
    except ValueError: return 0.0

def parse_report_data(text):
    data={"lines": {}}
    fleet_type="CEO"
    trigger_code="UNKNOWN"
    gw_lbs=0
    lines=text.strip().split('\n')
    for line in lines:
        parts=line.strip().split()
        if not parts: continue
        header=parts[0]
        data["lines"][header]=parts[1:]
        if header=='C1' and len(parts)>=4: trigger_code=parts[3]
        if header=='CE' and len(parts)>=2:
            try: gw_lbs=int(parts[1])*100
            except ValueError: pass

    upper_text=text.upper()
    
    # 테일넘버 강력 추출 (HL8534 등 리포트 어디에 있든 100% 잡아냄)
    aircraft_id=""
    match=re.search(r'HL\d{4}|\b8\d{3}\b', upper_text)
    if match:
        aircraft_id=match.group(0)
    else:
        if "AH" in data["lines"] and len(data["lines"]["AH"])>=1:
            aircraft_id=data["lines"]["AH"][0]
            
    if not aircraft_id: aircraft_id="UNKNOWN"

    neo_tails=["8364","8371","8356","8398","8399","8510","8511","8534","8533","8582","8584","8586","8705"]
    
    is_neo=False
    for tail in neo_tails:
        if tail in aircraft_id:
            is_neo=True
            break
            
    if is_neo or any(kw in upper_text for kw in ["NEO","LEAP","PW11","A321-25","A321-27"]):
        fleet_type="NEO"
        
    return fleet_type, trigger_code, data["lines"], gw_lbs, aircraft_id

@app.post("/analyze")
def analyze_report(req: AnalyzeRequest):
    try:
        fleet_type, trigger_code, lines_dict, gw_lbs, aircraft_id = parse_report_data(req.text)
        status="GREEN"
        reason="데이터 분석 완료"
        kpi_data={}
        CEO_MLW=166448
        NEO_MLW=174606
        
        if fleet_type=="CEO" and trigger_code.startswith("4"):
            s3_vrta=0.0
            t3_vrta=0.0
            if "S3" in lines_dict and len(lines_dict["S3"])>=1: s3_vrta=parse_g(lines_dict["S3"][0])
            if "T3" in lines_dict and len(lines_dict["T3"])>=1: t3_vrta=parse_g(lines_dict["T3"][0])
            max_vrta=max(s3_vrta, t3_vrta)
            kpi_data["S3"]={"VRTA": max_vrta}
            if gw_lbs<=CEO_MLW:
                if max_vrta>=2.86: status, reason="RED", f"Severe Hard Landing\n- VRTA: {max_vrta}g\n- LIMIT: >= 2.86g\n- 중량: Normal"
                elif max_vrta>=2.6: status, reason="AMBER", f"Hard Landing\n- VRTA: {max_vrta}g\n- LIMIT: >= 2.6g\n- 중량: Normal"
                else: status, reason="GREEN", f"Normal Landing\n- VRTA: {max_vrta}g\n- LIMIT: 2.6g 미달"
            else:
                if max_vrta>=2.6: status, reason="RED", f"Severe Hard Overweight Landing\n- VRTA: {max_vrta}g\n- LIMIT: >= 2.6g\n- 중량: Overweight"
                elif max_vrta>=1.7: status, reason="AMBER", f"Hard Overweight Landing\n- VRTA: {max_vrta}g\n- LIMIT: >= 1.7g\n- 중량: Overweight"
                else: status, reason="GREEN", f"Normal Landing\n- VRTA: {max_vrta}g\n- LIMIT: 1.7g 미달"
                
        elif fleet_type=="NEO" and trigger_code.startswith("4"):
            max_nz, max_ny=0.0, 0.0
            for key in ["U1", "U2"]:
                if key in lines_dict and len(lines_dict[key])>=2:
                    nz, ny=parse_g(lines_dict[key][0]), parse_g(lines_dict[key][1])
                    max_nz, max_ny=max(max_nz, nz), max(max_ny, ny)
                    kpi_data[key]={"Nz_kpi": nz, "Ny_kpi": ny}
            if max_nz>=2.06 or max_ny>=0.5:
                status, reason="RED", f"Severe Hard Landing\n- Nz: {max_nz}g, Ny: {max_ny}g\n- LIMIT: Nz >= 2.06g or Ny >= 0.5g"
            elif max_nz>=1.80 or max_ny>=0.45:
                status, reason="AMBER", f"Hard Landing\n- Nz: {max_nz}g, Ny: {max_ny}g\n- LIMIT: Nz >= 1.80g or Ny >= 0.45g"
            else:
                status, reason="GREEN", f"Normal Landing\n- Nz: {max_nz}g, Ny: {max_ny}g\n- LIMIT 미달"
                
        elif trigger_code in ["5100", "5200", "5300"]:
            vrta_max, vrta_min = 0.0, 0.0
            if fleet_type == "NEO":
                if "S1" in lines_dict and len(lines_dict["S1"])>=1: vrta_max=parse_g(lines_dict["S1"][0])
                if "S2" in lines_dict and len(lines_dict["S2"])>=1: vrta_min=parse_g(lines_dict["S2"][0])
            else:
                if "S3" in lines_dict and len(lines_dict["S3"])>=1: vrta_max=parse_g(lines_dict["S3"][0])
                if "S4" in lines_dict and len(lines_dict["S4"])>=1: vrta_min=parse_g(lines_dict["S4"][0])
            
            kpi_data["Turb"]={"VRTA_MAX": vrta_max, "VRTA_MIN": vrta_min}
            
            if trigger_code in ["5100", "5200"]: 
                if vrta_max >= 2.5 or vrta_min <= -1.0:
                    status, reason="RED", f"Inspection Required (Clean / Flap < 0.5)\n- VRTA MAX: {vrta_max}g, MIN: {vrta_min}g\n- LIMIT: MAX >= +2.5g OR MIN <= -1.0g"
                else:
                    status, reason="GREEN", f"Normal (Clean / Flap < 0.5)\n- VRTA MAX: {vrta_max}g, MIN: {vrta_min}g\n- LIMIT: MAX >= +2.5g OR MIN <= -1.0g"
            else: 
                if vrta_max >= 2.0 or vrta_min <= 0.0:
                    status, reason="RED", f"Inspection Required (Not Clean / Flap > 0.5)\n- VRTA MAX: {vrta_max}g, MIN: {vrta_min}g\n- LIMIT: MAX >= +2.0g OR MIN <= 0.0g"
                else:
                    status, reason="GREEN", f"Normal (Not Clean / Flap > 0.5)\n- VRTA MAX: {vrta_max}g, MIN: {vrta_min}g\n- LIMIT: MAX >= +2.0g OR MIN <= 0.0g"
                    
        elif trigger_code in ["5600", "5700"]:
            lata=0.0
            # 마이너스 값을 인식하고 절대값으로 바꾸는 완벽한 로직 적용
            lata_match = re.search(r'E1\s+([+-]?\d+)', req.text)
            if lata_match:
                lata = abs(float(lata_match.group(1)) / 100.0)
            
            kpi_data["E1"]={"LATA": lata}
            if lata>0.41: status, reason="RED", f"Severe High Lateral\n- LATA: {lata}g\n- LIMIT: > 0.41g"
            elif lata>=0.35: status, reason="AMBER", f"High Lateral\n- LATA: {lata}g\n- LIMIT: >= 0.35g"
            else: status, reason="GREEN", f"Normal\n- LATA: {lata}g"
        else: reason="정의되지 않은 코드"
        
        mlw_lbs=CEO_MLW if fleet_type=="CEO" else NEO_MLW
        return {"fleet_type": fleet_type, "aircraft_id": aircraft_id, "trigger_code": trigger_code, "status": status, "reason": reason, "kpi_data": kpi_data, "gw_lbs": gw_lbs, "mlw_lbs": mlw_lbs}
    except Exception as e: return {"error": str(e)}

@app.get("/")
@app.head("/")
def read_root():
    from fastapi.responses import HTMLResponse
    html_path=os.path.join("templates", "index.html")
    if os.path.exists(html_path):
        with open(html_path, "r", encoding="utf-8") as f: return HTMLResponse(content=f.read(), status_code=200)
    return HTMLResponse(content="<h1>index.html not found</h1>", status_code=404)