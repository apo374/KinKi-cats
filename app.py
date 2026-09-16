from flask import Flask, jsonify, request, render_template, session, redirect, url_for
from urllib.parse import urlparse, urljoin, quote
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
_geocode_cache = {}

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


def is_valid_coordinate(latitude, longitude):
    """緯度・経度が地図に描画できる数値か確認する"""
    try:
        latitude = float(latitude)
        longitude = float(longitude)
    except (TypeError, ValueError):
        return False
    return (math.isfinite(latitude) and math.isfinite(longitude)
            and -90 <= latitude <= 90 and -180 <= longitude <= 180)


def get_map_data():
    """地図に描画可能な避難所と被害・発信情報を返す"""
    current_shelters = load_json(DATA_FILE, [])
    current_instructions = load_json(INSTRUCTIONS_FILE, [])
    map_shelters = [
        {
            'id': shelter.get('id'),
            'name': shelter.get('name', '名称未設定'),
            'place_name': shelter.get('place_name', ''),
            'latitude': float(shelter['latitude']),
            'longitude': float(shelter['longitude']),
            'status': shelter.get('status', '利用状況不明')
        }
        for shelter in current_shelters
        if isinstance(shelter, dict)
        if is_valid_coordinate(shelter.get('latitude'), shelter.get('longitude'))
    ]
    incidents = [
        {
            'id': notice.get('id'),
            'title': notice.get('content', '発信情報'),
            'shelter': notice.get('shelter', ''),
            'status': notice.get('status', ''),
            'place_name': notice.get('place_name', ''),
            'updated_at': notice.get('updated_at', ''),
            'latitude': float(notice['latitude']),
            'longitude': float(notice['longitude'])
        }
        for notice in current_instructions
        if isinstance(notice, dict) and notice.get('target') == '住民'
        if is_valid_coordinate(notice.get('latitude'), notice.get('longitude'))
        and notice.get('status') not in ('解除', '完了')
    ]
    return {'shelters': map_shelters, 'incidents': incidents}


@app.route('/api/map_data')
def api_map_data():
    response = jsonify(get_map_data())
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.route('/api/geocode')
def api_geocode():
    """住所検索を気象庁以外の外部サービスへ中継する"""
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify({'error': '検索語を入力してください。'}), 400
    if len(query) > 100:
        return jsonify({'error': '検索語が長すぎます。'}), 400

    if query in _geocode_cache:
        return jsonify(_geocode_cache[query])

    url = (
        'https://nominatim.openstreetmap.org/search?format=jsonv2&limit=5&accept-language=ja&q='
        + quote(query)
    )
    try:
        request_obj = urllib.request.Request(
            url,
            headers={'User-Agent': 'bousai-app/1.0 contact: local-development'}
        )
        with urllib.request.urlopen(request_obj, timeout=8) as response:
            results = json.loads(response.read())
        locations = [
            {
                'name': result.get('display_name', query),
                'latitude': float(result['lat']),
                'longitude': float(result['lon'])
            }
            for result in results
            if is_valid_coordinate(result.get('lat'), result.get('lon'))
        ]
        payload = {'results': locations}
        _geocode_cache[query] = payload
        return jsonify(payload)
    except (OSError, ValueError, json.JSONDecodeError):
        return jsonify({'error': '場所を検索できませんでした。'}), 502


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

        latitude_value = request.form.get('latitude', '').strip()
        longitude_value = request.form.get('longitude', '').strip()
        if latitude_value or longitude_value:
            if not is_valid_coordinate(latitude_value, longitude_value):
                return render_template(
                    'shelter_register.html',
                    error=True,
                    message='緯度・経度は正しい数値で入力してください。'
                )

        next_id = max((shelter.get('id', 0) for shelter in shelters), default=0) + 1
        shelter = {'id': next_id, 'name': name}
        if latitude_value and longitude_value:
            shelter['latitude'] = float(latitude_value)
            shelter['longitude'] = float(longitude_value)
        shelters.append(shelter)
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


# 気象警報・注意報API
@app.route('/api/weather_warnings')
def api_weather_warnings():
    """気象警報・注意報をJSON形式で返すAPI"""
    return jsonify(get_weather_warnings(force_refresh=request.args.get('refresh') == '1'))

if __name__ == '__main__':
    app.run(debug=True, port=5000)
