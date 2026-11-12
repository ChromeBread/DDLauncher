"""純粋ロジックの最小テスト:  py -m pytest tests  （pytest 無しでも  py tests/test_core.py  で動く）"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config
from launcher_window import corner_pos
from obs_bridge import source_spec, unique_name


def test_corner_pos():
    area = (0, 0, 1920, 1040)                       # タスクバー下 40px
    assert corner_pos(area, (200, 60), "bottom_right", 6) == (1714, 974)
    assert corner_pos(area, (200, 60), "top_left", 6) == (6, 6)
    # 作業領域より大きくてもはみ出さない（左上に寄せる）
    assert corner_pos((100, 0, 150, 50), (200, 60), "bottom_right", 6) == (100, 0)
    # 2 枚目のモニタ（負座標）
    assert corner_pos((-1280, 0, 1280, 1024), (100, 40), "bottom_left", 0) == (-1280, 984)


def _overlap(a, b):
    return a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]


def test_beside_rect_never_overlaps_launcher():
    from launcher_window import beside_rect
    area = (0, 0, 1920, 1040)
    for launcher in [(1354, 923, 560, 117), (6, 6, 560, 117), (6, 923, 560, 117), (0, 0, 1920, 500)]:
        for size in [(1000, 500), (560, 600), (1900, 1000)]:
            r = beside_rect(area, launcher, size)
            assert not _overlap(r, launcher), (launcher, size, r)
            assert area[0] <= r[0] and r[0] + r[2] <= area[2] and r[1] + r[3] <= area[3], r
    # 右下ランチャーなら上に出て、サイズは縮めない
    assert beside_rect(area, (1354, 923, 560, 117), (1000, 460))[1:] == (455, 1000, 460)


def test_source_spec():
    assert source_spec(r"C:\a.PNG")[0] == "image_source"
    assert source_spec(r"C:\a.mp4")[1]["local_file"] == r"C:\a.mp4"
    assert source_spec(r"C:\a.wav")[0] == "ffmpeg_source"
    assert source_spec(r"C:\a.txt") is None


def test_unique_name():
    assert unique_name("bg", set()) == "bg"
    assert unique_name("bg", {"bg", "bg (2)"}) == "bg (3)"


def test_normalize_fills_defaults_and_keeps_user_values():
    cfg = config.normalize({"theme": "bogus", "launchers": [{"corner": "top_left", "apps": [{"name": "X"}]}]})
    assert cfg["theme"] == "opaque"
    assert cfg["obs"]["port"] == 4455
    l = cfg["launchers"][0]
    assert l["corner"] == "top_left" and l["mode"] == "normal"
    assert l["apps"][0]["name"] == "X" and l["apps"][0]["delay_sec"] == 0.0
    assert l["scale"] == 1.0
    assert config.normalize({"launchers": [{"scale": 9}]})["launchers"][0]["scale"] == 2.5
    assert config.normalize({"launchers": [{"scale": "x"}]})["launchers"][0]["scale"] == 1.0


def test_l_cells():
    from launcher_window import l_cells
    # 6 個: 角 + 横に 2 個、角の上に 3 個。最後の 1 個が角のすぐ上
    assert l_cells(6, 0, 0) == [(0, 0), (0, 1), (0, 2), (3, 0), (2, 0), (1, 0)]
    assert l_cells(6, 3, 4) == l_cells(6, 0, 0)
    assert l_cells(5, 4, 0) == [(0, 0), (0, 1), (0, 2), (0, 3), (1, 0)]   # 横 4、残りは角の上
    assert l_cells(3, 4, 0) == [(0, 0), (0, 1), (0, 2)]                   # 上限より少なければ横 1 列
    # 3x3 に 8 個: 外側の L 字（5 個）の内側に、もう 1 つの L 字
    assert l_cells(8, 3, 3) == [(0, 0), (0, 1), (0, 2), (2, 0), (1, 0), (1, 1), (1, 2), (2, 1)]
    cells = l_cells(10, 4, 2)                                             # 4x2 に入らない → 横に広げる
    assert len(set(cells)) == 10 and max(r for r, _ in cells) < 2
    assert l_cells(0, 4, 0) == []
    assert l_cells(4, 1, 0) == [(0, 0), (3, 0), (2, 0), (1, 0)]   # GROUP: 縦一列、最後が角のすぐ上


def test_linear_slots():
    from launcher_window import linear_slots
    apps = [{"name": "OBS"}, {"name": "YT", "group": "配信先"}, {"name": "VC"},
            {"name": "ニコ生", "group": "配信先"}, {"name": "X", "group": ""}, {"name": "Twitch", "group": "配信先"}]
    assert linear_slots(apps) == [[0], [1, 3, 5], [2], [4]]   # 同じジャンルは最初の 1 個の後ろに重なる


def test_normalize_layout_fields():
    l = config.normalize({"launchers": [{"layout": "x", "after_sequence": "x", "max_cols": "a", "max_rows": 99}]})["launchers"][0]
    assert (l["layout"], l["after_sequence"], l["max_cols"], l["max_rows"]) == ("group", "none", 4, 50)
    assert l["show_seq_button"] is False


def test_port_status():
    import i18n
    from winproc import port_status
    i18n.set_language("ja")
    assert port_status("obs64.exe", []) == ("free", "空き")
    i18n.set_language("en")
    assert port_status("obs64.exe", []) == ("free", "Free")
    assert port_status("OBS64.exe", [(10, "obs64.exe", "TCP")])[0] == "used"
    assert port_status("obs64.exe", [(11, "node.exe", "TCP")])[0] == "conflict"
    assert port_status("", [(11, "node.exe", "TCP")])[0] == "used"   # 想定プロセス未指定なら衝突扱いしない


def test_normalize_ports_drops_invalid():
    cfg = config.normalize({"ports": [{"port": "4455", "memo": "obs"}, {"port": ""}, {"port": 70000}]})
    assert cfg["ports"] == [{"port": 4455, "memo": "obs", "process": ""}]



def test_normalize_rotation():
    l = config.normalize({"launchers": [{"keep_rotation": 1, "rot": "x", "spins": {"a": 1}}]})["launchers"][0]
    assert (l["keep_rotation"], l["rot"], l["spins"]) == (True, 0, {})
    l = config.normalize({"launchers": [{"rot": "3", "spins": {"配信先": "2"}}]})["launchers"][0]
    assert (l["keep_rotation"], l["rot"], l["spins"]) == (False, 3, {"配信先": 2})



def test_migrate_legacy():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        old, new = os.path.join(d, "DDLauncher"), os.path.join(d, "CBApps", "DDLauncher")
        assert not config.migrate_legacy(old, new)                      # 旧設定が無ければ何もしない
        os.makedirs(os.path.join(old, "favicons"))
        with open(os.path.join(old, "config.json"), "w", encoding="utf-8") as f:
            f.write('{"theme": "transparent"}')
        open(os.path.join(old, "favicons", "a.ico"), "wb").close()
        assert config.migrate_legacy(old, new)
        with open(os.path.join(new, "config.json"), encoding="utf-8") as f:
            assert '"transparent"' in f.read()
        assert os.path.exists(os.path.join(new, "favicons", "a.ico"))
        assert os.path.exists(os.path.join(old, "config.json"))         # 旧フォルダは残す
        with open(os.path.join(new, "config.json"), "w", encoding="utf-8") as f:
            f.write("{}")
        assert not config.migrate_legacy(old, new)                      # 2 回目以降は上書きしない
        with open(os.path.join(new, "config.json"), encoding="utf-8") as f:
            assert f.read() == "{}"


def test_locales_complete():
    """全言語で、キーと {差し込み} が English と同じか。ヘルプも全言語あるか。"""
    import json
    import re
    import i18n
    def load(code):
        with open(os.path.join(i18n.LOCALE_DIR, f"{code}.json"), encoding="utf-8") as f:
            return json.load(f)
    en = load("en")
    slots = lambda s: sorted(re.findall(r"{(\w+)}", s))
    for code in i18n.LANGS:
        t = load(code)
        assert set(t) == set(en), (code, set(en) ^ set(t))
        for k, v in t.items():
            assert slots(v) == slots(en[k]), (code, k)
        assert os.path.exists(os.path.join(i18n.LOCALE_DIR, "help", f"{code}.html")), code
    assert list(i18n.LANGS) == config.LANGUAGES


def test_language_tags():
    import i18n
    assert [i18n.from_tag(t) for t in ("ja-JP", "en-US", "ko-KR", "id-ID", "fr-CA", "zh-TW", "zh_HK")] == \
        ["ja", "en", "ko", "id", "fr", "zh_TW", "zh_TW"]
    assert i18n.from_tag("zh-CN") is None and i18n.from_tag("de-DE") is None and i18n.from_tag("") is None
    assert i18n.from_tag("in_ID") == "id"
    assert i18n.resolve("fr") == "fr"
    i18n.set_language("ko")
    assert i18n.tr("menu.quit") == "종료" and i18n.tr("no.such.key") == "no.such.key"
    assert i18n.tr("status.launched", name="OBS") == "OBS 실행"
    i18n.set_language("en")


def test_normalize_language():
    assert config.normalize({})["language"] == "auto"
    assert config.normalize({"language": "fr"})["language"] == "fr"
    assert config.normalize({"language": "de"})["language"] == "auto"


def test_format_date():
    import datetime
    from about import format_date
    d = datetime.date(2012, 1, 12)
    assert format_date(d, "ja") == "2012年1月12日"
    assert format_date(d, "en") == "January 12, 2012"
    assert format_date(d, "ko") == "2012년 1월 12일"
    assert format_date(d, "id") == "12 Januari 2012"
    assert format_date(d, "fr") == "12 janvier 2012"
    assert format_date(datetime.date(2026, 8, 1), "fr") == "1er août 2026"
    assert format_date(d, "zh_TW") == "2012年1月12日"


def test_export_import_sections():
    cfg = config.normalize({"theme": "transparent", "obs": {"password": "secret"}, "ports": [{"port": 9000}]})
    data = config.export_data(cfg, ["general", "ports"])
    assert data["sections"] == ["ports", "general"] and "obs" not in data and "launchers" not in data
    base = config.normalize({})
    new, found = config.import_data(base, data)
    assert found == ["ports", "general"]
    assert new["theme"] == "transparent" and new["ports"][0]["port"] == 9000
    assert new["obs"]["password"] == ""                                   # 書き出していない項目は今の設定のまま
    new, found = config.import_data(base, {"obs": {"password": "x"}})      # config.json 形式でも読める
    assert found == ["obs"] and new["obs"]["password"] == "x" and new["obs"]["port"] == 4455
    assert config.import_data(base, {"nothing": 1})[1] == []
    assert config.import_data(base, [1, 2])[1] == []


def test_backup_rotation():
    import tempfile
    trashed = []
    real = config.move_to_trash
    config.move_to_trash = lambda p: (trashed.append(os.path.basename(p)), os.remove(p))   # 本物のゴミ箱を汚さない
    try:
        with tempfile.TemporaryDirectory() as d:
            cfg_path, bdir = os.path.join(d, "config.json"), os.path.join(d, "backups")
            assert config.backup_current(bdir, cfg_path) is None             # 設定が無ければ何もしない
            with open(cfg_path, "w") as f:
                f.write("{}")
            os.makedirs(bdir)
            for i in range(6):
                open(os.path.join(bdir, f"config-2020010{i}-000000.json"), "w").close()
            open(os.path.join(bdir, "keep-me.txt"), "w").close()             # 自分で作っていないファイル
            assert config.backup_current(bdir, cfg_path, keep=5)
            left = sorted(os.listdir(bdir))
            assert len([f for f in left if f.startswith("config-")]) == 5 and "keep-me.txt" in left
            assert trashed == ["config-20200100-000000.json", "config-20200101-000000.json"]
    finally:
        config.move_to_trash = real


def test_normalize_collapse():
    assert config.normalize({"launchers": [{}]})["launchers"][0]["collapse"] == "hover"   # 既定はアイコンのみ
    assert config.normalize({"launchers": [{"collapse": "always"}]})["launchers"][0]["collapse"] == "always"
    assert config.normalize({"launchers": [{"collapse": "x"}]})["launchers"][0]["collapse"] == "hover"


def test_fmt_bytes():
    from obs_bridge import fmt_bytes
    assert (fmt_bytes(0), fmt_bytes(2048), fmt_bytes(5 * 1024 ** 3), fmt_bytes(3 * 1024 ** 4)) == ("0B", "2.0KB", "5.0GB", "3072.0GB")


def test_uninstaller_only_touches_own_files():
    import tempfile
    import uninstaller
    d = tempfile.mkdtemp()
    for n in ["DDLauncher.exe", "DDLauncher-0.0.9.exe", "OBS.exe", "notes.txt", "DDLauncher.txt"]:
        open(os.path.join(d, n), "w").close()
    os.mkdir(os.path.join(d, "DDLauncher-dir.exe"))   # フォルダは exe 名でも対象外
    assert [os.path.basename(p) for p in uninstaller.find_exes(d)] == ["DDLauncher-0.0.9.exe", "DDLauncher.exe"]
    assert uninstaller.is_allowed(config.CONFIG_DIR) and uninstaller.is_allowed(config.LEGACY_DIR)
    assert not uninstaller.is_allowed(os.path.dirname(config.CONFIG_DIR))   # CBApps は他のソフトも使う
    assert not uninstaller.is_allowed(d)


def test_detect_app_ports():
    import json
    import tempfile
    import winproc
    cfgf = os.path.join(tempfile.mkdtemp(), "config.json")
    with open(cfgf, "w") as f:
        json.dump({"server_port": 4460}, f)
    orig = winproc.obs_websocket_port
    winproc.obs_websocket_port = lambda path=cfgf: orig(path)
    try:
        apps = [{"name": "OBS", "path": r"C:\x\obs64.exe"}, {"name": "VTS", "path": r"C:\y\VTube Studio.exe"}, {"name": "", "path": ""}]
        listening = {8001: [(10, "VTube Studio.exe", "TCP")], 80: [(11, "other.exe", "TCP")]}
        got = winproc.detect_app_ports(apps, listening)
        assert [(p["port"], p["process"]) for p in got] == [("4460", "obs64.exe"), ("8001", "vtube studio.exe")], got
    finally:
        winproc.obs_websocket_port = orig
    assert winproc.obs_websocket_port(os.path.join(tempfile.mkdtemp(), "none.json")) is None


def test_group_cells():
    from launcher_window import group_cells, l_cells
    assert group_cells(5, 0, 0) == l_cells(5, 1, 0)                 # 上限なしは今までの縦一列のまま
    assert group_cells(5, 0, 9) == l_cells(5, 1, 0)
    assert group_cells(5, 0, 2) == [(0, 0), (1, 0), (0, 1), (1, 1), (0, 2)]
    assert group_cells(7, 2, 2) == [(0, 0), (3, 0), (2, 0), (1, 0), (0, 1), (2, 1), (1, 1)]   # 横 2 列に抑えて縦を伸ばす
    cells = group_cells(13, 3, 4)
    assert len(set(cells)) == 13 and max(c for _, c in cells) <= 2
    assert group_cells(0, 3, 3) == []


def test_classify_ip_mode():
    from winproc import classify_ip_mode
    def a(t=6, up=True, dhcp=True, gw=True, desc="Realtek"):
        return {"type": t, "up": up, "dhcp": dhcp, "gateway": gw, "desc": desc}
    assert classify_ip_mode([a()]) == "dynamic"
    assert classify_ip_mode([a(dhcp=False), a(t=24, dhcp=True)]) == "static"           # ループバックは見ない
    assert classify_ip_mode([a(), a(t=71, dhcp=False, desc="Wi-Fi")]) == "mixed"
    assert classify_ip_mode([a(dhcp=False, desc="Hyper-V Virtual Ethernet"), a()]) == "dynamic"   # 仮想は除外
    assert classify_ip_mode([a(gw=False), a(up=False)]) is None                          # 外へ出られない / 切断
    assert classify_ip_mode([]) is None


def test_normalize_new_launcher_keys():
    l = config.normalize({"launchers": [{"mini_icon": 5, "wheel_rotate": 0, "layout": "linear"}]})["launchers"][0]
    assert (l["mini_icon"], l["wheel_rotate"], l["layout"]) == ("", False, "group")


def test_known_app_ports():
    import json
    import tempfile
    import i18n
    import winproc
    i18n.set_language("en")
    d = tempfile.mkdtemp()
    bdir = os.path.join(d, "棒読み ちゃん")   # 日本語・スペースを含むパス
    os.makedirs(bdir)
    setting = os.path.join(bdir, "BouyomiChan.setting")
    with open(setting, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0"?><Settings><EnableSocket>true</EnableSocket><PortNumber>50001</PortNumber>'
                '<EnableHttpd>true</EnableHttpd><PortNumberHttp>50090</PortNumberHttp></Settings>')
    assert winproc.bouyomi_setting_ports(setting) == [(50001, "Socket"), (50090, "HTTP")]
    assert winproc.bouyomi_setting_ports(os.path.join(d, "none.setting")) == []
    oc = {"config": {"integration": {"bouyomichan": {"enabled": True, "port": 50080,
                                                       "exePath": os.path.join(bdir, "BouyomiChan.exe")}}}}
    occ, yncc = os.path.join(d, "oc.json"), os.path.join(d, "ync.config")
    with open(occ, "w", encoding="utf-8") as f:
        json.dump(oc, f)
    with open(yncc, "w", encoding="utf-8") as f:
        json.dump({"dynamicPortMode": True}, f)
    saved = (winproc.ONECOMME_CONFIG, winproc.YNC_CONFIG, winproc.obs_websocket_port, winproc._running_exe_paths)
    winproc.ONECOMME_CONFIG, winproc.YNC_CONFIG, winproc.obs_websocket_port, winproc._running_exe_paths = occ, yncc, lambda: None, lambda exe: []   # この PC で動いているソフトを読まない
    try:
        found, notes = winproc.scan_known_ports([], {})
        assert [(p["port"], p["src"]) for p in found] == [("11180", "default"), ("50001", "config"), ("50090", "config")]
        assert len(notes) == 2 and "50080" in notes[0] and "Detect" in notes[1]   # 送り先ポートの食い違い + ゆかコネ停止中
        found, notes = winproc.scan_known_ports([], {15520: [(5, "YNC_Neo.exe", "TCP")]})
        ync = [p for p in found if p["port"] == "15520"]
        assert ync and ync[0]["src"] == "running" and "auto" in ync[0]["memo"]   # 自動で選ぶ設定であることを出す
        assert len(notes) == 2 and "next time" in notes[1]   # 食い違い + 番号が変わりうる案内
        assert winproc.port_status("YNC_Neo.exe", [], winproc.ports_of("YNC_Neo.exe", {15520: [(5, "YNC_Neo.exe", "TCP")]})) \
            == ("moved", "Port changed: now 15520")
        assert winproc.port_status("", [], [])[0] == "free"
    finally:
        winproc.ONECOMME_CONFIG, winproc.YNC_CONFIG, winproc.obs_websocket_port, winproc._running_exe_paths = saved
    assert winproc.onecomme_bouyomi(None) == ("", None) and winproc.ync_dynamic_port(None) is False
    sample = ("Processes:\n    ID: 32312, image: D:\\棒読み\\BouyomiChan.exe\nRegistered URLs:\n        HTTP://LOCALHOST:50080/\n"
              "    ID: 35072, image: D:\\YNC_Neo\\YNC_Neo.exe\n    URL group ID: FC00000020000001\n"
              "        HTTP://[::1]:11901:[::1]/\n        HTTP://LOCALHOST:11901/\n        HTTP://127.0.0.1:15520:127.0.0.1/\n")
    assert winproc.parse_http_sys(sample) == {50080: [32312], 11901: [35072], 15520: [35072]}
    assert winproc.parse_http_sys("        ID: 35072、イメージ: D:\\YNC_Neo\\YNC_Neo.exe \n  HTTP://LOCALHOST:15520/\n") == {15520: [35072]}   # 日本語表示の Windows


def test_drop_panel_beside_launcher():
    from launcher_window import beside_rect, grip_corner
    area, launcher = (0, 0, 1920, 1040), (1800, 640, 120, 400)   # 右下のランチャー
    x, y, w, h = beside_rect(area, launcher, (760, 300), gap=0)
    assert y + h == 640 and x + w <= 1920                      # 上にくっつき、被らない
    assert grip_corner((x, y, w, h), launcher, "bottom_right") == (True, True)   # つまみはランチャーから遠い左上
    x, y, w, h = beside_rect(area, (1800, 0, 120, 400), (760, 300), gap=0)   # 右上 → 下にくっつく
    assert y == 400 and grip_corner((x, y, w, h), (1800, 0, 120, 400), "top_right") == (False, True)
    l = config.normalize({"launchers": [{"on_top": 0, "dnd_size": ["a"]}]})["launchers"][0]
    assert (l["on_top"], l["dnd_on_top"], l["dnd_size"]) == (False, True, [0, 0])
    assert config.normalize({"launchers": [{"dnd_size": [900, 99999]}]})["launchers"][0]["dnd_size"] == [900, 4000]


def test_drop_panel_free_position():
    from launcher_window import free_rect
    area, launcher = (0, 0, 1920, 1040), (1800, 640, 120, 400)
    assert free_rect(area, (100, 100), (760, 300), launcher) == (100, 100, 760, 300)
    assert free_rect(area, (1500, 100), (760, 300), launcher) == (1160, 100, 760, 300)   # 画面からはみ出さない
    assert free_rect(area, (1500, 700), (760, 300), launcher) == (1040, 700, 760, 300)  # 被る → 一番近い左の辺へ
    assert free_rect(area, (1700, 400), (760, 300), launcher) == (1160, 340, 760, 300)  # 上の辺の方が近い
    assert free_rect(area, (0, 0), (1920, 1040), launcher) is None                      # どこにも置けない → 隣へ
    assert free_rect(area, (1040, 740), (760, 300), launcher) == (1040, 740, 760, 300)  # くっつくのは良い
    l = config.normalize({"launchers": [{"dnd_pos": [1, "x"]}]})["launchers"][0]
    assert l["dnd_pos"] is None and config.normalize({"launchers": [{"dnd_pos": [5, 6]}]})["launchers"][0]["dnd_pos"] == [5, 6]
    assert config.normalize({"launchers": [{"dnd_pos": [5]}]})["launchers"][0]["dnd_pos"] is None


def test_obs_guard():
    import winproc
    app, obs = {"path": r"C:\x\chrome.exe", "obs_guard": True}, {"path": r"C:\o\obs64.exe", "obs_guard": True}
    assert winproc.obs_guarded(app, {"obs64.exe": [1]}) and not winproc.obs_guarded(app, {})
    assert not winproc.obs_guarded(dict(app, obs_guard=False), {"obs64.exe": [1]})
    assert not winproc.obs_guarded(obs, {"obs64.exe": [1]})   # OBS 自身は止めない
    assert config.normalize({"launchers": [{"apps": [{"name": "a"}]}]})["launchers"][0]["apps"][0]["obs_guard"] is True


def test_custom_colors():
    import theme
    cfg = config.normalize({"color_mode": "bogus", "colors": {"accent": "#FF0000", "bg": "red", "text": 5}})
    assert cfg["color_mode"] == "default" and cfg["colors"] == {"accent": "#ff0000"}
    assert config.normalize({"colors": "x"})["colors"] == {}
    theme.apply_colors({"accent": "#ff0000", "bg": "#102030", "nope": "#000000"}, 60)
    assert theme.C["accent"] == "#ff0000" and theme.C["bg_glass"] == "rgba(16, 32, 48, 153)" and "nope" not in theme.C
    assert theme.C["tile_glass"].endswith(", 173)")   # タイルは背景より少し濃い
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    q = theme.qcolor(theme.C["tile_glass"])   # QColor("rgba(...)") は無効 = 黒になるので自前で読む
    assert q.isValid() and q.alpha() == 173 and theme.qcolor("#102030").alpha() == 255
    theme.apply_colors(None)
    assert theme.C["bg_glass"].endswith(", 255)") and theme.C["tile_glass"].endswith(", 255)")   # 100 % = 不透明と同じ見た目
    assert {k: v for k, v in theme.C.items() if "glass" not in k} == {k: v for k, v in theme.DEFAULT_C.items() if "glass" not in k}
    # 不透明度: 旧版の「半透明」テーマは 60 % で引き継ぐ。範囲外は丸める
    assert config.normalize({"theme": "transparent"})["opacity"] == 60
    assert config.normalize({})["opacity"] == 100 and config.normalize({"opacity": 5})["opacity"] == 20
    assert config.normalize({"theme": "transparent", "opacity": 80})["opacity"] == 80
    from settings_dialog import clean_seconds   # 待ち時間の入力: 全角は半角に、「秒」などは消す
    assert [clean_seconds(t) for t in ("３秒", "1.5s", "２．５", "abc", "", "10")] == ["3", "1.5", "2.5", "0", "0", "10"]
    for name, colors in theme.TEMPLATES.items():   # テンプレートは全色そろっていて、normalize で落ちない
        assert list(colors) == theme.CUSTOM_KEYS, name
        assert config.normalize({"colors": colors})["colors"] == colors, name


def test_nexusfont_roundtrip():
    """書き出し → 別の場所へ読み込み。セットは Sets/<セット名>/<元の構造>、library.xml のパスが書き換わる。"""
    import tempfile
    import zipfile
    import nexusfont
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "元 フォント", "フリー")
        os.makedirs(src)
        for name in ("A.ttf", "B&C.otf"):
            with open(os.path.join(src, name), "wb") as f:
                f.write(name.encode() * 100)
        a, bc = os.path.join(src, "A.ttf"), os.path.join(src, "B&C.otf")
        ud = os.path.join(tmp, "nf", "userdata")
        os.makedirs(ud)
        crlf = chr(13) + chr(10)   # NexusFont の library.xml は CRLF
        xml = crlf.join(['<?xml version="1.0" encoding="UTF-8"?>', "<library>", "  <groups>", '    <group name="all">',
                         f"      <folder>{src}</folder>", "    </group>", "  </groups>", "  <sets>", '    <set name="空"/>',
                         '    <set name="作業1">', f"      <font>{a}</font>", f'      <font>{bc.replace("&", "&amp;")}</font>',
                         f'      <font>{os.path.join(tmp, "gone.ttf")}</font>', "    </set>", "  </sets>", "</library>", ""])
        with open(os.path.join(ud, "library.xml"), "w", encoding="utf-8", newline="") as f:
            f.write(xml)
        with open(os.path.join(ud, "nexusfont.ini"), "w", encoding="utf-8-sig", newline="") as f:
            f.write(crlf.join(["[Window]", "Left=2251", "[Display]", "Skin=default", ""]))
        lib = nexusfont.read_library(ud)
        assert lib["sets"]["作業1"][1] == bc and lib["folders"] == [src]
        plan = nexusfont.plan_export(lib, ["作業1"], {src: nexusfont.scan_folder(src)})
        assert plan["missing"] == [os.path.join(tmp, "gone.ttf")] and len(plan["entries"]) == 4
        assert plan["entries"][0][1] == "Sets/作業1/" + nexusfont.rel_path(a)
        assert nexusfont.measure_speed(plan["entries"], tmp) > 0 and len(os.listdir(tmp)) == 2   # 試し書きは残らない
        z = os.path.join(tmp, "out.zip")
        assert nexusfont.write_export(z, ud, plan)
        with zipfile.ZipFile(z) as zf:
            assert b"Left=" not in zf.read("nexusfont/nexusfont.ini")
        assert not nexusfont.write_export(z + "2", ud, plan, cancelled=lambda: True) and not os.path.exists(z + "2.tmp")

        ud2, dest = os.path.join(tmp, "pc2", "userdata"), os.path.join(tmp, "新しい PC")
        os.makedirs(ud2)
        with open(os.path.join(ud2, "nexusfont.ini"), "w", encoding="utf-8-sig", newline="") as f:
            f.write(crlf.join(["[Window]", "Left=10", ""]))
        with zipfile.ZipFile(z) as zf:
            m = nexusfont.read_manifest(zf)
            assert not m["asis"]
            p = nexusfont.plan_import(zf, m, dest)
            assert len(p["new"]) == 4 and not p["same"]
            assert nexusfont.extract(zf, p["new"], trash=lambda _: False)
            assert len(nexusfont.plan_import(zf, m, dest)["same"]) == 4   # 2 回目は全部「同じ」
            bdir = nexusfont.restore_data(zf, ud2, m, dest, os.path.join(tmp, "bk"))
        assert os.path.isfile(os.path.join(bdir, "nexusfont.ini"))
        with open(os.path.join(ud2, "library.xml"), encoding="utf-8", newline="") as f:
            new = f.read()
        new_a = os.path.join(dest, "Sets", "作業1", *nexusfont.rel_path(a).split("/"))
        assert f"<font>{new_a}</font>" in new and os.path.isfile(new_a)
        assert "B&amp;C.otf</font>" in new and crlf in new and '<set name="空"/>' in new
        assert f"<folder>{os.path.join(dest, 'Folders', *nexusfont.rel_path(src).split('/'))}</folder>" in new
        assert os.path.join(tmp, "gone.ttf") in new   # 入れていないものは元のパスのまま
        with open(os.path.join(ud2, "nexusfont.ini"), encoding="utf-8-sig") as f:
            ini = f.read()
        assert "Left=10" in ini and "Skin=default" in ini

        # 「そのまま」: 元のパスへ戻し、library.xml と ini（ウィンドウ位置）は書き換えない
        za = os.path.join(tmp, "asis.zip")
        assert nexusfont.write_export(za, ud, plan, asis=True)
        os.remove(a)   # 元の PC のフォントが 1 つ消えた状態から戻す
        ud3 = os.path.join(tmp, "pc3", "userdata")
        with zipfile.ZipFile(za) as zf:
            m = nexusfont.read_manifest(zf)
            assert m["asis"] and nexusfont.missing_drives(m) == []
            p = nexusfont.plan_import(zf, m, None)
            assert [t for _, t in p["new"]] == [a] and len(p["same"]) == 1   # セットとフォルダの同じファイルは 1 回だけ
            assert nexusfont.extract(zf, p["new"], trash=lambda _: False) and os.path.isfile(a)
            nexusfont.restore_data(zf, ud3, m, None, os.path.join(tmp, "bk"))
        with open(os.path.join(ud3, "library.xml"), encoding="utf-8", newline="") as f:
            assert f.read() == xml
        with open(os.path.join(ud3, "nexusfont.ini"), encoding="utf-8-sig") as f:
            assert "Left=2251" in f.read()
        assert nexusfont.missing_drives({"folders": {"Q:\\Fonts": "Folders/Fonts"}}) in ([], ["Q:"])


def test_nexusfont_window_fix():
    import nexusfont
    crlf = chr(13) + chr(10)
    ini = crlf.join(["[Window]", "Top=38", "Left=2251", "Width=1013", "Height=776", "[Display]", "Left=9", ""])
    three = [(0, 0, 1920, 1032), (3360, 0, 5280, 1032), (1920, 0, 3360, 852)]
    assert nexusfont.window_fix(ini, three) is None   # 3 枚目のモニタの中
    fixed = nexusfont.window_fix(ini, three[:1])      # モニタ 1 枚の PC では画面外 → 主モニタへ
    assert "Top=50" in fixed and "Left=50" in fixed and "Width=1013" in fixed and fixed.endswith("Left=9" + crlf)
    assert "Width=1300" in nexusfont.window_fix(ini.replace("Width=1013", "Width=4000"), [(0, 0, 1400, 900)])
    assert nexusfont.window_fix("[Display]" + crlf, three) is None


def test_sound_profiles():
    """.spr の読み込み、別 PC での名前合わせ、適用（Windows の操作は差し替え）、設定の補正。"""
    import sound
    crlf = chr(13) + chr(10)
    spr = crlf.join(["[ProfileItem0]", "ID={0.0.0.00000000}.{aaa}", "Name=Speakers", "Type=1", "Mute=0", "VolumeScalar=0.5000",
                     "DefaultRender=1", "DefaultRenderMultimedia=1", "DefaultRenderCommunications=0",
                     "[ProfileItem1]", "ID={0.0.1.00000000}.{bbb}", "Name=Mic", "Type=1", "Mute=1", "VolumeScalar=1.0000",
                     "DefaultCaptureCommunications=1",
                     "[ProfileItem2]", "ID={0.0.0.00000000}.{aaa}|app.exe", "Name=", "Type=3", "Mute=0", ""])
    p = sound.parse_spr(spr, "OBS")
    assert set(p["defaults"]["render"]) == {"console", "multimedia"} and list(p["defaults"]["capture"]) == ["communications"]
    assert [(l["flow"], l["mute"], l["volume"]) for l in p["levels"]] == [("render", False, 0.5), ("capture", True, 1.0)]
    devs = [{"id": "X", "name": "Speakers", "desc": "Speakers", "flow": "render"},   # 別の PC: ID は違い、名前は同じ
            {"id": "Y", "name": "Mic", "desc": "Mic", "flow": "capture"},
            {"id": "Z", "name": "Mic", "desc": "Mic", "flow": "render"}]               # 向きが違う同名は使わない
    calls = []
    tg, missing = sound.apply_profile(p, devs, lambda i, r: calls.append(("def", i, r)), lambda i, m, v: calls.append(("lv", i, m, v)))
    assert tg == {("render", "console"): "X", ("render", "multimedia"): "X", ("capture", "communications"): "Y"} and not missing
    assert ("lv", "Y", True, 1.0) in calls and ("def", "Y", 2) in calls
    assert sound.resolve({"id": "?", "name": "Mic", "flow": "capture"}, devs + [dict(devs[1], id="W")]) is None   # 同名が 2 つなら諦める
    assert sound.targets(p, [])[1] == ["Speakers", "Mic"]
    cap = sound.capture_profile("now", [dict(devs[0], mute=False, volume=0.3)], {"render": {"console": "X"}, "capture": {"console": "gone"}})
    assert cap["defaults"] == {"render": {"console": {"id": "X", "name": "Speakers"}}, "capture": {}}
    cfg = config.normalize({"sound": {"profiles": [p, "bad", {"x": 1}], "auto_restore": 1}})
    assert [q["name"] for q in cfg["sound"]["profiles"]] == ["OBS"] and cfg["sound"]["auto_restore"] is True
    assert config.normalize({"sound": "x"})["sound"]["profiles"] == []


def test_web_icon():
    """アイコン欄の URL: サイトはサイトごと、画像は URL ごとに保存。取れていないあいだは exe のアイコン。"""
    import sys
    import favicon
    assert favicon.is_image_url("https://example.com/a/logo.PNG?x=1") and not favicon.is_image_url("https://example.com/")
    assert favicon.cache_path("https://example.com/a") == favicon.cache_path("https://EXAMPLE.com/b")
    assert favicon.cache_path("https://example.com/1.png") != favicon.cache_path("https://example.com/2.png")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from launcher_window import load_app_icon
    url = "https://unused.invalid/never-fetched.png"
    assert not os.path.exists(favicon.cache_path(url))
    assert load_app_icon({"path": sys.executable, "icon": url}) is not None    # exe のアイコンに戻る
    assert load_app_icon({"path": "https://unused.invalid/", "icon": ""}) is None


def test_sequence_timeline():
    """時間グラフは Sequencer と同じ数え方: チェックありでパスがあるものだけ、前のアプリからの待ちを足していく。"""
    import sequencer
    apps = [{"autostart": True, "path": "a", "delay_sec": 0}, {"autostart": False, "path": "b", "delay_sec": 9},
            {"autostart": True, "path": "", "delay_sec": 9}, {"autostart": True, "path": "c", "delay_sec": 2.5},
            {"autostart": True, "path": "d", "delay_sec": "x"}]
    assert sequencer.timeline(apps) == [(0, 0.0, 0.0), (3, 2.5, 2.5), (4, 0.0, 2.5)]
    assert sequencer.timeline([]) == []
    import i18n
    from settings_dialog import fmt_sec
    i18n.set_language("ja")
    assert [fmt_sec(s) for s in (0, 2.5, 59.96, 60, 95, 120.5)] == ["0秒", "2.5秒", "1分", "1分", "1分35秒", "2分0.5秒"]
    i18n.set_language("en")
    assert fmt_sec(95) == "1 min 35 s"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")
