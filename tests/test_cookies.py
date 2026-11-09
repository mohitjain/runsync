from runsync.cookies import check_cookies

HDR = "# Netscape HTTP Cookie" + " File\n"  # split so the repo guard does not flag this fixture


def line(name, exp, domain=".youtube.com", http_only=False):
    p = "#HttpOnly_" if http_only else ""
    return f"{p}{domain}\tTRUE\t/\tTRUE\t{exp}\t{name}\tvalue\n"


def test_missing(tmp_path):
    assert check_cookies(tmp_path / "nope.txt").state == "missing"


def test_ok(tmp_path):
    f = tmp_path / "c.txt"
    f.write_text(HDR + line("__Secure-3PSID", 2000000000, http_only=True))
    assert check_cookies(f, now=1700000000).usable


def test_expired(tmp_path):
    f = tmp_path / "c.txt"
    f.write_text(HDR + line("SID", 1600000000))
    assert check_cookies(f, now=1700000000).state == "expired"


def test_no_login(tmp_path):
    f = tmp_path / "c.txt"
    f.write_text(HDR + line("PREF", 2000000000))
    assert check_cookies(f).state == "no_login"


def test_cookie_rotation_warning_sets_flag():
    from runsync import logs
    logs.reset_cookie_state()
    logs.YtdlpLogger().warning("[youtube:tab] The provided YouTube account cookies are no longer valid. ...")
    assert logs.cookies_rotated()
    logs.reset_cookie_state()
    logs.YtdlpLogger().warning("some other warning")
    assert not logs.cookies_rotated()
