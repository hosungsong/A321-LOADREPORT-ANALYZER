import re
from flask import Flask, request, jsonify, render_template

app = Flask(__name__)

# 원리: 리포트에 A321-NEO라고 명시되지 않은 경우를 대비해 기번만으로 기종을 식별합니다.
# 이유: 향후 CEO와 NEO의 특정 무게(GW) 제한이나 Spurious 판별 로직이 세분화될 때를 대비한 기초 작업입니다.
FLEET_DB = {
    "HL8398": "NEO", "HL8364": "NEO", "HL8356": "NEO", "HL8399": "NEO",
    "HL8371": "NEO", "HL8395": "NEO", "HL8510": "NEO", "HL8515": "NEO",
    # "HL7722": "CEO", "HL7763": "CEO"  <- 향후 CEO 기번들을 여기에 추가하면 됩니다.
}

# 원리: 리포트에 '183'이라고 적힌 것은 '1.83g'를, '025'는 '0.25g'를 의미합니다.
# 이유: 연산을 위해 문자열을 100으로 나누어 실제 물리량(g-force)으로 변환해야 합니다.
def parse_kpi_value(val_str):
    try:
        # 음수 처리 (예: -052 -> -0.52)
        if val_str.startswith('-'):
            return int(val_str) / 100.0
        return int(val_str) / 100.0
    except ValueError:
        return 0.0

# 원리: PDF에서 추출한 AMM 05-51-11-200-004-C 그래프 수치를 그대로 조건문(if-elif)으로 구현했습니다.
def evaluate_landing_severity(nz, ny):
    # Red Zone 판단 (수직가속도 2.06 이상 또는 측면가속도 0.5 이상)
    if nz >= 2.06 or ny >= 0.50:
        return "RED", "Severe Hard Landing / Severe Overweight Landing"
    
    # Amber Zone 판단 (수직 1.80~2.05 또는 측면 0.45~0.49)
    elif 1.80 <= nz < 2.06 or 0.45 <= ny < 0.50:
        return "AMBER", "Hard Landing / Hard Overweight Landing"
    
    # Green Zone 판단 (한계치 미만)
    else:
        return "GREEN", "Normal Landing (Limit Not Exceeded)"

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
