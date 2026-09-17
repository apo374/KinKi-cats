import io
import json
from urllib.parse import unquote

import pytest

from app import app, shelters


@pytest.fixture
def client():
    app.config['TESTING'] = True
    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess['logged_in'] = True
            sess['username'] = 'admin'
        yield client


@pytest.fixture
def clean_shelters():
    original = list(shelters)
    shelters.clear()
    shelters.extend([
        {
            'id': 1,
            'name': '既存避難所',
            'postal_code': '0300801',
            'address': '青森市役所前',
            'capacity': 50,
            'status': '空きあり',
            'male_toilet_count': 2,
            'female_toilet_count': 3,
            'accessible_toilet_count': 1,
            'barrier_free': 'あり',
            'pet_allowed': True,
            'image': '/uploads/sample.png'
        }
    ])
    yield shelters
    shelters.clear()
    shelters.extend(original)


def test_admin_gate_requires_login():
    with app.test_client() as client:
        resp = client.get('/shelter_register')
        assert resp.status_code == 302
        assert '/login' in resp.headers['Location']


def test_selection_screen_renders(client):
    resp = client.get('/shelter_register')
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert '新規登録・更新・削除を選んでください。' in html
    assert '/shelter_register/new' in html
    assert '/shelter_register/update' in html
    assert '/shelter_register/delete' in html


def test_shelter_management_form_does_not_show_walking_time(client):
    new_html = client.get('/shelter_register/new').get_data(as_text=True)
    update_html = client.get('/shelter_register/update').get_data(as_text=True)
    assert '徒歩時間' not in new_html
    assert '徒歩時間' not in update_html


def test_shelter_management_form_only_shows_symbol_statuses(client):
    html = client.get('/shelter_register/new').get_data(as_text=True)
    assert 'value="〇"' in html
    assert 'value="△"' in html
    assert 'value="×"' in html
    assert '空きあり' not in html
    assert '残りわずか' not in html
    assert '満員' not in html


def test_new_shelter_registration(client, clean_shelters):
    resp = client.post(
        '/shelter_register/new',
        data={
            'name': '新規避難所',
            'postal_code': '0300802',
            'address': '青森市新町1-1',
            'capacity': '80',
            'status': '△',
            'male_toilet_count': '2',
            'female_toilet_count': '2',
            'accessible_toilet_count': '1',
            'barrier_free': 'あり',
            'slope': 'あり',
            'wheelchair_accessible': 'なし',
            'pet_allowed': 'on',
            'image': (io.BytesIO(b'fake-image-content'), 'test.png', 'image/png')
        },
        content_type='multipart/form-data'
    )
    assert resp.status_code == 200
    assert '避難所を登録しました。' in resp.get_data(as_text=True)
    assert any(s['name'] == '新規避難所' for s in shelters)
    saved = next(s for s in shelters if s['name'] == '新規避難所')
    assert saved['slope'] == 'あり'
    assert saved['wheelchair_accessible'] == 'なし'
    assert f"/static{saved['image']}" in resp.get_data(as_text=True)


def test_missing_name_is_rejected(client, clean_shelters):
    resp = client.post(
        '/shelter_register/new',
        data={
            'postal_code': '0300802',
            'address': '青森市新町1-1',
            'capacity': '80',
            'status': '〇',
            'male_toilet_count': '1',
            'female_toilet_count': '1',
            'accessible_toilet_count': '1',
            'barrier_free': 'あり'
        }
    )
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert '建物名を入力してください。' in html


def test_update_route_loads_existing_values(client, clean_shelters):
    resp = client.get('/shelter_register/update?shelter_id=1')
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert '既存避難所' in html
    assert '青森市役所前' in html


def test_update_keeps_old_image_when_not_replaced(client, clean_shelters):
    clean_shelters[0]['walking_minutes'] = 12
    resp = client.post(
        '/shelter_register/update',
        data={
            'shelter_id': '1',
            'name': '更新後避難所',
            'postal_code': '0300803',
            'address': '青森市新町2-2',
            'capacity': '75',
            'status': '〇',
            'male_toilet_count': '3',
            'female_toilet_count': '4',
            'accessible_toilet_count': '2',
            'barrier_free': '一部あり',
            'pet_allowed': 'on'
        }
    )
    assert resp.status_code == 200
    updated = next(s for s in shelters if s['id'] == 1)
    assert updated['name'] == '更新後避難所'
    assert updated['image'] == '/uploads/sample.png'
    assert updated['walking_minutes'] == 12


def test_update_saves_accessibility_values_and_shows_them_selected(client, clean_shelters):
    response = client.post(
        '/shelter_register/update',
        data={
            'shelter_id': '1',
            'name': '更新後避難所',
            'status': '〇',
            'barrier_free': 'あり',
            'slope': 'あり',
            'wheelchair_accessible': 'なし',
        },
    )
    assert response.status_code == 200
    assert clean_shelters[0]['slope'] == 'あり'
    assert clean_shelters[0]['wheelchair_accessible'] == 'なし'
    html = response.get_data(as_text=True)
    assert '<option value="あり" selected>あり</option>' in html
    assert '<option value="なし" selected>なし</option>' in html


def test_delete_route_requires_confirmation_and_removes_record(client, clean_shelters):
    resp = client.post('/shelter_register/delete', data={'shelter_id': '1', 'confirm': 'true'})
    assert resp.status_code == 200
    assert '避難所を削除しました。' in resp.get_data(as_text=True)
    assert all(s['id'] != 1 for s in shelters)


def test_postal_code_lookup_uses_server_proxy(monkeypatch, client):
    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload
        def read(self):
            return json.dumps(self.payload).encode('utf-8')
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc_val, exc_tb):
            return False

    def fake_urlopen(req, timeout=None, **kwargs):
        assert '0300801' in req.full_url
        assert timeout == 8
        return FakeResponse({
            'results': [{
                'address1': '青森県',
                'address2': '青森市',
                'address3': '本町',
                'address4': ''
            }]
        })

    monkeypatch.setattr('app.urllib.request.urlopen', fake_urlopen)
    resp = client.get('/api/postal_code_lookup?zipcode=0300801')
    assert resp.status_code == 200
    assert resp.json['address'] == '青森県 青森市 本町'


def test_postal_code_lookup_validates_length(client):
    resp = client.get('/api/postal_code_lookup?zipcode=1234')
    assert resp.status_code == 400
    assert '7桁' in resp.get_json()['error']


def test_geocode_returns_coordinates_from_address(monkeypatch, client):
    class FakeResponse:
        def read(self):
            return json.dumps([{
                'display_name': '青森県青森市新町',
                'lat': '40.8244',
                'lon': '140.7400'
            }]).encode('utf-8')
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc_val, exc_tb):
            return False

    def fake_urlopen(req, timeout=None, **kwargs):
        assert '青森市新町' in unquote(req.full_url)
        assert timeout == 8
        return FakeResponse()

    monkeypatch.setattr('app.urllib.request.urlopen', fake_urlopen)
    resp = client.get('/api/geocode?q=青森市新町')
    assert resp.status_code == 200
    assert resp.json['results'] == [{
        'name': '青森県青森市新町',
        'latitude': 40.8244,
        'longitude': 140.74
    }]


def test_geocode_prefers_gsi_address_search(monkeypatch, client):
    class FakeResponse:
        def read(self):
            return json.dumps([{
                'geometry': {'coordinates': [140.73587, 40.828262]},
                'properties': {'title': '青森県青森市新町一丁目１番'}
            }]).encode('utf-8')
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc_val, exc_tb):
            return False

    def fake_urlopen(req, timeout=None, **kwargs):
        assert 'msearch.gsi.go.jp' in req.full_url
        assert timeout == 8
        return FakeResponse()

    monkeypatch.setattr('app.urllib.request.urlopen', fake_urlopen)
    resp = client.get('/api/geocode?q=青森県青森市新町一丁目1番')
    assert resp.status_code == 200
    assert resp.json['results'][0]['latitude'] == 40.828262
    assert resp.json['results'][0]['longitude'] == 140.73587


def test_board_route_loads_excel_data(client):
    resp = client.get('/board')
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert '① 被害状況地図' in html
    assert '② 被害状況一覧' in html
    assert '③ 避難指示' in html
    assert '④ 派遣された職員の一覧' in html
    assert '⑤ 避難所利用登録状況' in html
    assert '青森市中央部' in html


def test_board_map_points_include_known_locations(client):
    resp = client.get('/board')
    payload = resp.get_data(as_text=True)
    assert '青森駅周辺' in payload
    assert '浪岡地区' in payload


def test_damage_map_points_use_valid_excel_coordinates_only():
    import app as app_module

    points = app_module.build_damage_map_points([
        {'場所': '任意の場所', '被害内容': '冠水', '状況': '確認中', '発生日時': '日時', '緯度': '40.8', '経度': '140.7'},
        {'場所': '不正な場所', '被害内容': '崩落', '状況': '確認中', '発生日時': '日時', '緯度': '91', '経度': '140.7'},
        {'場所': '座標なし', '被害内容': '停電', '状況': '確認中', '発生日時': '日時'},
    ])
    assert len(points) == 1
    assert points[0]['location'] == '任意の場所'
    assert points[0]['latitude'] == 40.8
    assert points[0]['longitude'] == 140.7


def test_home_map_data_includes_board_damage_and_shelters(monkeypatch, client):
    import app as app_module

    original_load_json = app_module.load_json

    def fake_load_json(path, default):
        if path == app_module.DATA_FILE:
            return [{'id': 1, 'name': '既存避難所', 'latitude': 40.8244, 'longitude': 140.7400}]
        if path == app_module.INSTRUCTIONS_FILE:
            return []
        return original_load_json(path, default)

    monkeypatch.setattr(app_module, 'load_json', fake_load_json)
    resp = client.get('/api/map_data')
    assert resp.status_code == 200
    payload = resp.get_json()
    assert any(item['name'] == '既存避難所' for item in payload['shelters'])
    assert any(item['place_name'] == '青森駅周辺' and item['title'] == '道路冠水'
               for item in payload['incidents'])


def test_board_shows_registered_shelter_status(client, clean_shelters):
    clean_shelters[0]['capacity'] = 50
    with client.session_transaction() as sess:
        sess['registered_shelter_id'] = 1
        sess['registered_shelter_count'] = 3
    response = client.get('/board')
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert '既存避難所' in html
    assert '47人' in html
    assert '94.0%' in html
    assert '>〇<' in html


def test_board_hides_deleted_registered_shelter(client, clean_shelters):
    with client.session_transaction() as sess:
        sess['registered_shelter_id'] = 999
    response = client.get('/board')
    assert '利用登録された避難所はありません' in response.get_data(as_text=True)


def test_home_shows_active_evacuation_instructions(monkeypatch, client):
    monkeypatch.setattr('app.load_board_data', lambda: {
        '避難指示': [{
            '対象地域': '青森市中央部',
            '指示内容': '高台へ避難してください',
            '発令日時': '2026年9月17日 10:00',
            '状況': '発令中',
        }],
    })
    response = client.get('/')
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert '避難指示: 高台へ避難してください' in html
    assert '対象地域: 青森市中央部' in html


def test_search_page_is_public_and_renders_form(client):
    resp = client.get('/shelter_search')
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert '避難所の建物名' in html
    assert 'いずれかの情報を入力してください。' in html
    assert 'placeholder="例：〇〇市〇〇町"' in html
    assert 'その他要望' in html
    assert 'ペット受け入れ可否' in html
    assert 'バリアフリー対応' in html
    assert 'class="management-form shelter-search-form"' in html
    assert 'class="form-grid"' in html
    assert '住所検索' in html
    assert '住所(現在地でも避難所でも可)' in html
    assert 'pet-allowed' in html
    assert 'barrier_free' in html
    assert 'id="all-shelters-link"' in html
    assert '現在地を取得して一覧を表示中...' in html


def test_all_shelters_uses_current_location_for_sorting(client, clean_shelters):
    shelters.clear()
    shelters.extend([
        {'id': 1, 'name': '遠い施設', 'latitude': 40.9, 'longitude': 140.9, 'status': '〇'},
        {'id': 2, 'name': '近い施設', 'latitude': 40.825, 'longitude': 140.74, 'status': '〇'},
    ])
    response = client.get('/all_shelters?current_lat=40.8244&current_lng=140.7400')
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert html.index('近い施設') < html.index('遠い施設')


def test_search_navigation_and_results_do_not_show_registration_links(client):
    search_html = client.get('/shelter_search').get_data(as_text=True)
    results_html = client.get('/search_results').get_data(as_text=True)
    assert 'href="/all_shelters">避難所検索結果表示</a>' not in search_html
    assert '避難所を登録する' not in results_html
    assert results_html.count('>並び替え</span>') == 1
    assert 'class="sr-only"' not in results_html


def test_search_results_filter_by_multiple_fields(client, clean_shelters):
    shelters.clear()
    shelters.extend([
        {
            'id': 1,
            'name': '中央公園避難所',
            'postal_code': '030-0001',
            'address': '青森市中央町1-1',
            'capacity': 80,
            'pet_allowed': True,
            'barrier_free': True,
        },
        {
            'id': 2,
            'name': '駅前支所',
            'postal_code': '030-0002',
            'address': '青森市駅前町2-2',
            'capacity': 15,
            'pet_allowed': False,
            'barrier_free': False,
        },
    ])
    resp = client.get('/search_results?name=中央&capacity=20&pet_allowed=yes&barrier_free=yes')
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert '中央公園避難所' in html
    assert '該当する避難所が見つかりませんでした' not in html


def test_search_summary_shows_pet_and_barrier_free_conditions(client, clean_shelters):
    response = client.get('/search_results?pet_allowed=yes&barrier_free=no')
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert 'ペット受け入れ「可」' in html
    assert 'バリアフリー「未対応」' in html


def test_search_results_exclude_full_shelters(client, clean_shelters):
    shelters.clear()
    shelters.extend([
        {'id': 1, 'name': '空きあり避難所', 'status': '〇'},
        {'id': 2, 'name': '満員避難所', 'status': '×'},
    ])
    response = client.get('/search_results')
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert '空きあり避難所' in html
    assert '満員避難所' not in html


def test_all_shelters_exclude_full_shelters(client, clean_shelters):
    shelters.clear()
    shelters.extend([
        {'id': 1, 'name': '空きあり避難所', 'status': '〇'},
        {'id': 2, 'name': '満員避難所', 'status': '×'},
    ])
    response = client.get('/all_shelters')
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert '空きあり避難所' in html
    assert '満員避難所' not in html


def test_search_results_no_match_message(client):
    shelters.clear()
    shelters.extend([
        {'id': 1, 'name': '避難所A', 'address': '青森市', 'capacity': 5, 'pet_allowed': False, 'barrier_free': False},
    ])
    resp = client.get('/search_results?name=見つからない')
    assert resp.status_code == 200
    assert '該当する避難所が見つかりませんでした' in resp.get_data(as_text=True)


def test_search_uses_independent_building_postal_address_conditions(client, clean_shelters):
    shelters.clear()
    shelters.extend([
        {'id': 1, 'name': '中央公園', 'postal_code': '030-0001', 'address': '青森市中央町', 'capacity': 100},
        {'id': 2, 'name': '駅前学校', 'postal_code': '030-0002', 'address': '青森市駅前町', 'capacity': 120},
    ])
    response = client.get('/search_results?building_name=中央&postal_code=0300001&address=中央町')
    assert response.status_code == 200
    assert '中央公園' in response.get_data(as_text=True)
    assert '駅前学校' not in response.get_data(as_text=True)


def test_current_location_sorts_by_distance(client, clean_shelters):
    shelters.clear()
    shelters.extend([
        {'id': 1, 'name': '遠い施設', 'latitude': 40.9, 'longitude': 140.9, 'capacity': 10},
        {'id': 2, 'name': '近い施設', 'latitude': 40.825, 'longitude': 140.74, 'capacity': 10},
    ])
    response = client.get('/search_results?current_lat=40.8244&current_lng=140.7400&sort=distance')
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert html.index('近い施設') < html.index('遠い施設')
    assert '0.1 km' in html


def test_address_search_sorts_shelters_by_distance(monkeypatch, client, clean_shelters):
    shelters.clear()
    shelters.extend([
        {'id': 1, 'name': '遠い避難所', 'address': '青森市', 'latitude': 40.9, 'longitude': 140.9, 'status': '〇'},
        {'id': 2, 'name': '近い避難所', 'address': '青森市', 'latitude': 40.825, 'longitude': 140.74, 'status': '〇'},
    ])

    class FakeResponse:
        def read(self):
            return json.dumps([{
                'geometry': {'coordinates': [140.7405, 40.8245]},
                'properties': {'title': '入力住所'}
            }]).encode('utf-8')
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc_val, exc_tb):
            return False

    def fake_urlopen(req, timeout=None, **kwargs):
        assert 'msearch.gsi.go.jp' in req.full_url
        return FakeResponse()

    monkeypatch.setattr('app.urllib.request.urlopen', fake_urlopen)
    response = client.get('/search_results?address=青森市入力住所')
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert html.index('近い避難所') < html.index('遠い避難所')
    assert 'current_lat=40.8245' in html


def test_shelter_detail_and_unknown_id(client, clean_shelters):
    response = client.get('/shelters/1')
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert '既存避難所' in html
    assert 'class="secondary-button detail-back-button"' in html
    assert '検索結果へ戻る' in html
    assert 'id="user-count"' in html
    assert 'class="detail-registration-form"' in html
    assert client.get('/shelters/999').status_code == 404


def test_shelter_use_registration_keeps_shelter_data(client, clean_shelters):
    original = dict(clean_shelters[0])
    response = client.post('/shelters/1/use', data={
        'user_count': '3',
        'current_lat': '40.82',
        'current_lng': '140.74',
    })
    assert response.status_code == 302
    assert response.headers['Location'].startswith('/shelters/1?')
    assert clean_shelters[0] == original

    detail = client.get(response.headers['Location'])
    assert '利用登録する' in detail.get_data(as_text=True)
    assert 'この避難所を利用登録しました。' in detail.get_data(as_text=True)
    with client.session_transaction() as sess:
        assert sess['registered_shelter_count'] == 3


def test_shelter_use_registration_requires_positive_count(client, clean_shelters):
    response = client.post('/shelters/1/use', data={'user_count': '0'})
    assert response.status_code == 302
    detail = client.get(response.headers['Location'])
    assert '利用人数は1人以上で入力してください。' in detail.get_data(as_text=True)


def test_shelter_detail_hides_image_fallback_until_image_fails(client, clean_shelters):
    clean_shelters[0]['image'] = '/uploads/shelters/existing.png'
    response = client.get('/shelters/1')
    html = response.get_data(as_text=True)
    assert 'class="photo-fallback photo-load-fallback" hidden' in html
