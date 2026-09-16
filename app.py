from flask import Flask, jsonify, request, render_template, session, redirect, url_for
from urllib.parse import urlparse, urljoin
from functools import wraps
import json
import math
import os
import time
import urllib.request
from datetime import datetime, timedelta, timezone

# app.py はプロジェクト直下に置く。
# 実体（templates / static / data）は bousai_app/ 配下にあるので、そこを参照する。
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(BASE_DIR, 'bousai_app')

app = Flask(
    __name__,
    template_folder=os.path.join(APP_DIR, 'templates'),
    static_folder=os.path.join(APP_DIR, 'static'),
)
app.secret_key = 'your-secret-key-here'

# 管理者認証情報
ADMIN_CREDENTIALS = {
    'admin': '123'
}

# ────────────────────────────────
# 気象警報・注意報設定
PREFECTURE_CODE = "020000"  # 青森県
AREA_NAME = "青森市"

# 気象庁の警報・注意報JSONにおける青森市の市区町村コード
AREA_CODE = "0220100"

WARNING_URL = (
    f"https://www.jma.go.jp/bosai/warning/data/r8/{PREFECTURE_CODE}.json"
)

JST = timezone(timedelta(hours=9))
WEATHER_CACHE_SECONDS = 600
_weather_cache = None
_weather_cache_at = 0.0

# 警報・注意報のコード一覧
WARNING_CODES = {
    "00": "解除",
    "02": "暴風雪警報",
    "03": "レベル3大雨警報",
    "04": "洪水警報",
    "05": "暴風警報",
    "06": "大雪警報",
    "07": "波浪警報",
    "08": "レベル3高潮警報",
    "09": "レベル3土砂災害警報",
    "10": "レベル2大雨注意報",
    "12": "大雪注意報",
    "13": "風雪注意報",
    "14": "雷注意報",
    "15": "強風注意報",
    "16": "波浪注意報",
    "17": "融雪注意報",
    "18": "洪水注意報",
    "19": "レベル2高潮注意報",
    "20": "濃霧注意報",
    "21": "乾燥注意報",
    "22": "なだれ注意報",
    "23": "低温注意報",
    "24": "霜注意報",
    "25": "着氷注意報",
    "26": "着雪注意報",
    "27": "その他の注意報",
    "29": "レベル2土砂災害注意報",
    "32": "暴風雪特別警報",
    "33": "レベル5大雨特別警報",
    "35": "暴風特別警報",
    "36": "大雪特別警報",
    "37": "波浪特別警報",
    "38": "レベル5高潮特別警報",
    "39": "レベル5土砂災害特別警報",
    "43": "レベル4大雨危険警報",
    "48": "レベル4高潮危険警報",
    "49": "レベル4土砂災害危険警報"
}

# ────────────────────────────────
# サンプルデータの読み込み
DATA_FILE = os.path.join(APP_DIR, 'data', 'shelters.json')
# ホームの地図が参照する被害情報。登録処理は発信ボード側で実装する。
DAMAGE_FILE = os.path.join(APP_DIR, 'data', 'damage_reports.json')
INSTRUCTIONS_FILE = os.path.join(APP_DIR, 'data', 'instructions.json')

def load_json(path, default):
    """JSONファイルを読み込む（存在しない・壊れている場合は default を返す）"""
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default

shelters = load_json(DATA_FILE, [])
instructions = load_json(INSTRUCTIONS_FILE, [])

def save_instructions():
    """指示ボードのデータをファイルに保存する"""
    try:
        with open(INSTRUCTIONS_FILE, 'w', encoding='utf-8') as f:
            json.dump(instructions, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def save_shelters():
    """避難所データをファイルに保存する"""
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(shelters, f, ensure_ascii=False, indent=2)


def has_valid_coordinates(item):
    try:
        latitude = float(item['latitude'])
        longitude = float(item['longitude'])
    except (KeyError, TypeError, ValueError):
        return False
    return (math.isfinite(latitude) and math.isfinite(longitude)
            and -90 <= latitude <= 90 and -180 <= longitude <= 180)
# ────────────────────────────────

# ────────────────────────────────
# 認証関連の設定とヘルパー関数
def is_safe_url(target):
    """リダイレクト先URLが安全かどうかチェック"""
    ref_url = urlparse(request.host_url)
    test_url = urlparse(urljoin(request.host_url, target))
    return test_url.scheme in ('http', 'https') and ref_url.netloc == test_url.netloc

def login_required(f):
    """認証が必要なページに付けるデコレータ"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            # 現在のURLをnextパラメータとしてログイン画面にリダイレクト
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

def get_japan_time():
    """日本時間（JST）の現在時刻を取得する"""
    return datetime.now(JST).strftime("%Y年%m月%d日 %H:%M")


def format_report_time(iso_str):
    """気象庁の発表時刻（ISO形式）をJSTの表示用文字列に変換する"""
    if not iso_str:
        return "不明"
    try:
        parsed = datetime.fromisoformat(iso_str.replace('Z', '+00:00'))
        if parsed.tzinfo:
            parsed = parsed.astimezone(JST)
        return parsed.strftime("%Y年%m月%d日 %H:%M")
    except ValueError:
        return iso_str


def filter_shelters(district=None):
    """district 指定があれば一致する避難所のみ、なければ全件を返す"""
    return [s for s in shelters if not district or s.get('district') == district]


def parse_area_warnings(warning_data):
    """気象庁JSONの最新報から対象市区町村の発表・継続中の情報を抽出する"""
    if not isinstance(warning_data, list):
        raise ValueError("気象庁の警報・注意報データが新形式の配列ではありません")

    def report_datetime(report):
        value = report.get("reportDatetime", "")
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (AttributeError, ValueError):
            return datetime.min.replace(tzinfo=timezone.utc)

    relevant_reports = []
    for report in warning_data:
        if not isinstance(report, dict):
            continue
        warning = report.get("warning")
        if not isinstance(warning, dict):
            continue
        class20_items = warning.get("class20Items", [])
        if not isinstance(class20_items, list):
            continue
        if any(
            isinstance(item, dict) and item.get("areaCode") == AREA_CODE
            for item in class20_items
        ):
            relevant_reports.append(report)

    if not relevant_reports:
        return [], ""

    latest_datetime = max(report_datetime(report) for report in relevant_reports)
    latest_reports = [
        report for report in relevant_reports
        if report_datetime(report) == latest_datetime
    ]
    warnings = []
    seen_codes = set()

    for report in latest_reports:
        area = next(
            item for item in report["warning"]["class20Items"]
            if isinstance(item, dict) and item.get("areaCode") == AREA_CODE
        )
        for kind in area.get("kinds", []):
            if not isinstance(kind, dict):
                continue
            status = kind.get("status", "").strip()
            code = kind.get("code", "")
            if status not in ("発表", "継続") or not code or code in seen_codes:
                continue
            warnings.append({
                "name": WARNING_CODES.get(
                    code,
                    f"不明な警報・注意報 (コード: {code})"
                ),
                "code": code,
                "status": status
            })
            seen_codes.add(code)

    return warnings, latest_datetime.isoformat()


def get_weather_warnings(force_refresh=False):
    """対象市区町村の警報・注意報を取得する"""
    global _weather_cache, _weather_cache_at

    now = time.monotonic()
    if not force_refresh and _weather_cache is not None and now - _weather_cache_at < WEATHER_CACHE_SECONDS:
        return _weather_cache

    try:
        # 青森県の新形式（令和8年～）警報・注意報データを取得
        with urllib.request.urlopen(url=WARNING_URL, timeout=10) as res:
            warning_data = json.loads(res.read())

        warnings, report_datetime = parse_area_warnings(warning_data)

        result = {
            "area_name": AREA_NAME,
            "warnings": warnings,
            "report_time": format_report_time(report_datetime),
            "last_fetch_time": get_japan_time()
        }
        _weather_cache = result
        _weather_cache_at = now
        return result

    except Exception:
        return {
            "area_name": AREA_NAME,
            "warnings": [],
            "report_time": "取得失敗",
            "last_fetch_time": get_japan_time(),
            "error": True
        }


# トップページ：templates/index.html を返す（住民向け指示も表示する）
@app.route('/')
def index():
    resident_notices = [
        i for i in load_json(INSTRUCTIONS_FILE, []) if i.get('target') == '住民'
    ]
    return render_template('index.html', resident_notices=resident_notices)

# ログインページ
@app.route('/login', methods=['GET', 'POST'])
def login():
    # リダイレクト先を取得（デフォルトは避難所登録画面）
    next_url = request.args.get('next') or request.form.get('next')

    # 安全でないURLの場合はデフォルトページにリダイレクト
    if not next_url or not is_safe_url(next_url):
        next_url = url_for('shelter_register')

    if request.method == 'POST':
        password = request.form.get('password', '').strip()

        # 認証チェック
        username = next(
            (name for name, registered_password in ADMIN_CREDENTIALS.items()
             if registered_password == password),
            None
        )
        if username:
            session['logged_in'] = True
            session['username'] = username
            # ログイン成功後は指定されたページにリダイレクト
            return redirect(next_url)
        return render_template('login.html', error=True, message="パスワードが正しくありません。", next=next_url)

    # ログイン済みの場合は指定されたページにリダイレクト
    if session.get('logged_in'):
        return redirect(next_url)

    return render_template('login.html', next=next_url)

# ログアウト
@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

# 避難所登録ページ※user が避難所登録ページについて具体的に修正指示しない限り、このコードは正しいのでこのまま保持すること。
@app.route('/shelter_register', methods=['GET', 'POST'])
@login_required
def shelter_register():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        if not name:
            return render_template(
                'shelter_register.html',
                error=True,
                message='避難所名を入力してください。'
            )

        next_id = max((shelter.get('id', 0) for shelter in shelters), default=0) + 1
        shelters.append({'id': next_id, 'name': name})
        try:
            save_shelters()
        except OSError:
            shelters.pop()
            return render_template(
                'shelter_register.html',
                error=True,
                message='避難所情報を保存できませんでした。'
            )

        return render_template(
            'shelter_register.html',
            success=True,
            message=f'避難所「{name}」を登録しました。'
        )

    return render_template('shelter_register.html')

# 避難所検索ページ
@app.route('/shelter_search')
def shelter_search():
    return render_template('shelter_search.html')

# 全施設一覧ページ
@app.route('/all_shelters')
def all_shelters():
    return render_template('search_results.html', results=shelters)


# 指示ボード：住民向けの指示を一覧で確認する
@app.route('/board')
@login_required
def board():
    resident_instructions = [i for i in instructions if i.get('target') == '住民']
    return render_template('board.html', instructions=resident_instructions)

# 検索結果ページ：templates/search_results.html を返す
@app.route('/search_results')
def search_results():
    results = filter_shelters(request.args.get('district'))
    return render_template('search_results.html', results=results)

# JSON API：/shelters?district=地区名
@app.route('/shelters', methods=['GET'])
def get_shelters():
    results = filter_shelters(request.args.get('district'))

    if not results:
        # 見つからなければエラー JSON を返す
        return jsonify({'error': 'No shelters found'}), 404

    # 見つかったらリストを JSON で返す
    return jsonify(results)


@app.route('/api/map_data')
def api_map_data():
    """ホーム用の読み取りAPI。

    発信ボード側から受け取る被害情報は damage_reports.json の配列を想定する。
    地図に必要な項目は title, latitude, longitude, status（発生中/解消）。
    place_name, description, updated_at は任意。
    避難所は shelters.json の name, latitude, longitude を参照し、place_name は任意。
    """
    try:
        with open(DATA_FILE, encoding='utf-8') as f:
            current_shelters = json.load(f)
        with open(DAMAGE_FILE, encoding='utf-8') as f:
            current_reports = json.load(f)
    except (OSError, json.JSONDecodeError):
        return jsonify({'error': '登録情報を読み込めませんでした'}), 503
    if not isinstance(current_shelters, list) or not isinstance(current_reports, list):
        return jsonify({'error': '登録情報の形式が正しくありません'}), 503
    mapped_shelters = [
        {'id': item.get('id'), 'name': item.get('name', ''),
         'place_name': item.get('place_name', ''),
         'latitude': float(item['latitude']), 'longitude': float(item['longitude'])}
        for item in current_shelters if isinstance(item, dict) and has_valid_coordinates(item)
    ]
    active_reports = [
        {'id': item.get('id'), 'title': item.get('title', ''),
         'description': item.get('description', ''),
         'place_name': item.get('place_name', ''),
         'latitude': float(item['latitude']), 'longitude': float(item['longitude']),
         'updated_at': item.get('updated_at', '')}
        for item in current_reports
        if isinstance(item, dict) and item.get('status') == '発生中' and has_valid_coordinates(item)
    ]
    response = jsonify({'shelters': mapped_shelters, 'damage_reports': active_reports})
    response.headers['Cache-Control'] = 'no-store'
    return response

# 気象警報・注意報API
@app.route('/api/weather_warnings')
def api_weather_warnings():
    """気象警報・注意報をJSON形式で返すAPI"""
    return jsonify(get_weather_warnings(force_refresh=request.args.get('refresh') == '1'))

if __name__ == '__main__':
    app.run(debug=True, port=5000)
