#!/usr/bin/env python3
"""司机社自动签到。

设计目标：
- 从受信任的官方发布源自动发现 HTTPS 论坛地址；
- 在提交账号密码前验证目标确实具有司机社/Discuz 论坛特征；
- 使用适合 GitHub Actions Ubuntu Runner 的无头 Chrome；
- 任何关键步骤失败时返回非零退出码。
"""

from __future__ import annotations

import html as html_lib
import ipaddress
import os
import re
import shutil
import socket
import sys
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import requests
from lxml import html
from requests.adapters import HTTPAdapter
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from urllib3.util.retry import Retry

DEFAULT_RELEASE_SOURCES = (
    "https://47447.net/",
    "https://t.me/s/xsijishe",
)
SIGN_PATH = "/k_misign-sign.html"
LOGIN_PATH = "/member.php?mod=logging&action=login"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "15"))
PAGE_TIMEOUT = int(os.getenv("PAGE_TIMEOUT", "30"))
MAX_FORUM_CANDIDATES = 4
LOGIN_ATTEMPTS = 5

BRAND_MARKERS = ("司机社", "sijishe")
FORUM_MARKERS = (
    "discuz",
    "member.php?mod=logging",
    "home.php?mod=space",
    "k_misign",
    "formhash",
    "seccode",
)
SIGNED_MARKERS = ("今日已签", "您今天已经签到过了")
SUCCESS_MARKERS = ("签到成功", "恭喜您签到成功")
LOGIN_SUCCESS_MARKERS = ("退出", "欢迎您回来", "spacecp.php", "action=logout")
LOGIN_FAILURE_MARKERS = (
    "密码错误",
    "登录失败",
    "验证码填写错误",
    "抱歉，密码错误",
)


class CheckinError(RuntimeError):
    """可安全展示给 Actions 日志的签到错误。"""


@dataclass(frozen=True)
class Account:
    username: str
    password: str


def mask_username(username: str) -> str:
    """在日志中隐藏大部分账号信息。"""
    if len(username) <= 2:
        return "**"
    return f"{username[0]}***{username[-1]}"


def parse_accounts(raw_value: str) -> list[Account]:
    """解析 XSIJISHE；多账号使用换行分隔，避免误拆邮箱账号。"""
    if not raw_value or not raw_value.strip():
        raise CheckinError("未配置 GitHub Secret：XSIJISHE")

    accounts: list[Account] = []
    for index, entry in enumerate(raw_value.splitlines(), start=1):
        entry = entry.strip()
        if not entry:
            continue
        username, separator, password = entry.partition("&")
        if not separator or not username.strip() or not password:
            raise CheckinError(f"XSIJISHE 中第 {index} 个账号格式错误，应为：账号&密码")
        accounts.append(Account(username=username.strip(), password=password))

    if not accounts:
        raise CheckinError("XSIJISHE 中没有可用账号")
    return accounts


def normalize_base_url(raw_url: str) -> str | None:
    """把候选链接规范为 HTTPS 站点根地址。"""
    value = html_lib.unescape(raw_url).replace("\\/", "/").strip()
    value = value.rstrip(".,;:!?)）】]}\"'")
    if value.startswith("//"):
        value = "https:" + value

    try:
        parsed = urlparse(value)
    except ValueError:
        return None

    if parsed.scheme.lower() != "https" or not parsed.hostname:
        return None
    if parsed.username or parsed.password:
        return None

    host = parsed.hostname.lower().rstrip(".")
    if host == "localhost" or host.endswith(".local"):
        return None
    try:
        literal_ip = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host:
            return None
    else:
        if not literal_ip.is_global:
            return None

    port = f":{parsed.port}" if parsed.port and parsed.port != 443 else ""
    return f"https://{host}{port}"


def host_resolves_publicly(base_url: str) -> bool:
    """拒绝解析到私有、回环或保留网络的候选域名。"""
    parsed = urlparse(base_url)
    host = parsed.hostname
    if not host:
        return False
    try:
        addresses = socket.getaddrinfo(
            host,
            parsed.port or 443,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror:
        return False

    resolved = {item[4][0] for item in addresses}
    if not resolved:
        return False
    try:
        return all(ipaddress.ip_address(address).is_global for address in resolved)
    except ValueError:
        return False


def extract_https_candidates(page_text: str, source_url: str) -> list[str]:
    """从发布页 HTML、正文和脚本中提取 HTTPS 候选站点。"""
    if not page_text:
        return []

    source_host = (urlparse(source_url).hostname or "").lower()
    excluded_hosts = {
        source_host,
        "t.me",
        "telegram.me",
        "telegram.org",
        "www.telegram.org",
        "twitter.com",
        "x.com",
        "discord.com",
        "github.com",
        "cloudflare.com",
        "www.cloudflare.com",
    }

    raw_urls: list[str] = []
    normalized_text = html_lib.unescape(page_text).replace("\\/", "/")

    try:
        document = html.fromstring(normalized_text)
        for href in document.xpath("//a[@href]/@href"):
            absolute = urljoin(source_url, href)
            if absolute.lower().startswith("https://"):
                raw_urls.append(absolute)
    except (ValueError, TypeError):
        pass

    raw_urls.extend(
        re.findall(r"https://[^\s<>\"']+", normalized_text, flags=re.IGNORECASE)
    )

    candidates: list[str] = []
    seen: set[str] = set()
    for raw_url in raw_urls:
        candidate = normalize_base_url(raw_url)
        if not candidate:
            continue
        host = (urlparse(candidate).hostname or "").lower()
        if host in excluded_hosts or host.endswith(".telegram.org"):
            continue
        if candidate not in seen:
            seen.add(candidate)
            candidates.append(candidate)
    return candidates


def has_forum_fingerprint(page_text: str, final_url: str = "") -> bool:
    """确认页面同时具有司机社品牌和 Discuz/签到技术特征。"""
    haystack = f"{page_text}\n{final_url}".lower()
    has_brand = any(marker.lower() in haystack for marker in BRAND_MARKERS)
    has_forum = any(marker.lower() in haystack for marker in FORUM_MARKERS)
    return has_brand and has_forum


def build_http_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9"}
    )
    retry = Retry(
        total=2,
        connect=2,
        read=2,
        backoff_factor=0.6,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD"}),
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def locate_chrome_binary() -> str | None:
    configured = os.getenv("CHROME_BIN", "").strip()
    if configured:
        return configured
    for name in (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
    ):
        path = shutil.which(name)
        if path:
            return path
    return None


def create_webdriver() -> webdriver.Chrome:
    options = webdriver.ChromeOptions()
    chrome_binary = locate_chrome_binary()
    if chrome_binary:
        options.binary_location = chrome_binary
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-extensions")
    options.add_argument("--window-size=1920,1080")
    options.add_argument(f"--user-agent={USER_AGENT}")
    options.page_load_strategy = "eager"

    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(PAGE_TIMEOUT)
    return driver


@contextmanager
def managed_webdriver() -> Iterator[webdriver.Chrome]:
    driver = create_webdriver()
    try:
        yield driver
    finally:
        driver.quit()


def wait_for_document(driver: webdriver.Chrome, timeout: int = 15) -> None:
    WebDriverWait(driver, timeout).until(
        lambda current: (
            current.execute_script("return document.readyState")
            in {"interactive", "complete"}
        )
    )


def browser_page(
    driver: webdriver.Chrome, url: str, pause: float = 1.5
) -> tuple[str, str]:
    driver.get(url)
    wait_for_document(driver)
    if pause:
        time.sleep(pause)
    return driver.page_source, driver.current_url


def get_release_sources() -> tuple[str, ...]:
    configured = os.getenv("FORUM_RELEASE_URLS", "").strip()
    if not configured:
        return DEFAULT_RELEASE_SOURCES

    sources: list[str] = []
    for entry in re.split(r"[,;\r\n]+", configured):
        normalized = normalize_base_url(entry.strip())
        if normalized:
            sources.append(entry.strip())
    return tuple(sources) or DEFAULT_RELEASE_SOURCES


def collect_candidates(
    driver: webdriver.Chrome, session: requests.Session, sources: Iterable[str]
) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()

    for source in sources:
        source_candidates: list[str] = []
        try:
            response = session.get(source, timeout=HTTP_TIMEOUT, allow_redirects=True)
            if response.ok:
                source_candidates.extend(
                    extract_https_candidates(response.text, source)
                )
            else:
                print(
                    f"发布源返回 HTTP {response.status_code}，尝试浏览器模式：{source}"
                )
        except requests.RequestException as exc:
            print(
                "普通请求无法读取发布源，尝试浏览器模式："
                f"{source}（{type(exc).__name__}）"
            )

        if not source_candidates:
            try:
                page_text, final_url = browser_page(driver, source, pause=2.5)
                source_candidates.extend(extract_https_candidates(page_text, source))
                if normalize_base_url(final_url) != normalize_base_url(source):
                    source_candidates.insert(0, final_url)
            except Exception as exc:
                # 只记录异常类型，不输出可能含页面数据的详情。
                print(f"浏览器无法读取发布源：{source}（{type(exc).__name__}）")

        for candidate in source_candidates:
            normalized = normalize_base_url(candidate)
            if normalized and normalized not in seen:
                seen.add(normalized)
                candidates.append(normalized)

    return candidates


def validate_candidate_with_requests(
    session: requests.Session, candidate: str
) -> str | None:
    """不携带账号信息验证候选站点，成功时返回最终 HTTPS 根地址。"""
    if not host_resolves_publicly(candidate):
        return None
    combined_text = ""
    final_base = candidate
    for path in ("/", LOGIN_PATH):
        try:
            response = session.get(
                urljoin(candidate + "/", path.lstrip("/")),
                timeout=HTTP_TIMEOUT,
                allow_redirects=True,
            )
        except requests.RequestException:
            return None

        if response.status_code >= 400:
            return None
        normalized_final = normalize_base_url(response.url)
        if not normalized_final:
            return None
        final_base = normalized_final
        combined_text += "\n" + response.text[:1_000_000]

    if has_forum_fingerprint(combined_text, final_base):
        return final_base
    return None


def validate_candidate_with_browser(
    driver: webdriver.Chrome, candidate: str
) -> str | None:
    if not host_resolves_publicly(candidate):
        return None
    try:
        page_text, final_url = browser_page(
            driver,
            urljoin(candidate + "/", LOGIN_PATH.lstrip("/")),
            pause=1.5,
        )
    except Exception:
        return None

    final_base = normalize_base_url(final_url)
    if final_base and has_forum_fingerprint(page_text, final_url):
        return final_base
    return None


def resolve_forum_urls(driver: webdriver.Chrome) -> list[str]:
    """返回通过安全验证的论坛地址，供登录失败时依次切换。"""
    session = build_http_session()
    configured_url = os.getenv("FORUM_URL", "").strip()

    if configured_url:
        normalized = normalize_base_url(configured_url)
        if not normalized:
            raise CheckinError("FORUM_URL 必须是有效的 HTTPS 地址")
        candidates = [normalized]
        print("检测手动指定的论坛地址")
    else:
        print("正在从官方发布源自动查询 HTTPS 论坛地址")
        candidates = collect_candidates(driver, session, get_release_sources())
        if not candidates:
            raise CheckinError(
                "官方发布源没有返回候选地址；可在 GitHub Variables 中临时设置 FORUM_URL"
            )

    print(f"发现 {len(candidates)} 个 HTTPS 候选地址，开始安全验证")
    validated_candidates: list[str] = []
    for index, candidate in enumerate(candidates, start=1):
        print(f"验证候选地址 {index}/{len(candidates)}：{candidate}")
        validated = validate_candidate_with_requests(session, candidate)
        if not validated:
            validated = validate_candidate_with_browser(driver, candidate)
        if validated and validated not in validated_candidates:
            validated_candidates.append(validated)
            print(f"有效论坛地址 {len(validated_candidates)}：{validated}")
            if configured_url or len(validated_candidates) >= MAX_FORUM_CANDIDATES:
                break

    if validated_candidates:
        print(f"共保留 {len(validated_candidates)} 个有效论坛地址")
        return validated_candidates

    raise CheckinError(
        "没有找到通过 HTTPS、证书和论坛页面特征验证的地址；不会提交账号密码"
    )


def resolve_forum_url(driver: webdriver.Chrome) -> str:
    """兼容只需要一个论坛地址的调用方。"""
    return resolve_forum_urls(driver)[0]


def find_first(driver: webdriver.Chrome, selectors: Iterable[tuple[str, str]]):
    for by, value in selectors:
        elements = driver.find_elements(by, value)
        for element in elements:
            if element.is_displayed():
                return element
    return None


def solve_captcha(driver: webdriver.Chrome) -> str | None:
    input_element = find_first(
        driver,
        (
            (By.NAME, "seccodeverify"),
            (By.CSS_SELECTOR, "input[name*='seccodeverify']"),
        ),
    )
    if input_element is None:
        return None

    image_element = find_first(
        driver,
        (
            (By.CSS_SELECTOR, "img[src*='seccode']"),
            (By.CSS_SELECTOR, "span[id^='seccode_'] img"),
            (By.CSS_SELECTOR, "img[id^='vseccode_']"),
        ),
    )
    if image_element is None:
        raise CheckinError("登录页要求验证码，但没有找到验证码图片")

    try:
        WebDriverWait(driver, 10).until(
            lambda current: current.execute_script(
                "return arguments[0].complete && arguments[0].naturalWidth > 0",
                image_element,
            )
        )
    except TimeoutException as exc:
        raise CheckinError("验证码图片加载超时") from exc

    # 延迟加载可避免在不需要验证码时初始化体积较大的 OCR 模型。
    import ddddocr

    ocr = ddddocr.DdddOcr(show_ad=False)
    result = ocr.classification(image_element.screenshot_as_png).strip()
    result = re.sub(r"\s+", "", result)
    if not result:
        raise CheckinError("验证码识别结果为空")
    input_element.clear()
    input_element.send_keys(result)
    return result


def page_indicates_logged_in(page_text: str) -> bool:
    lowered = page_text.lower()
    return any(marker.lower() in lowered for marker in LOGIN_SUCCESS_MARKERS)


def driver_indicates_logged_in(driver: webdriver.Chrome) -> bool:
    """结合页面和认证 Cookie 判断登录状态，避免被页面内的失败文案误判。"""
    if page_indicates_logged_in(driver.page_source):
        return True
    cookie_names = {cookie.get("name", "").lower() for cookie in driver.get_cookies()}
    return any("auth" in name for name in cookie_names)


def login(driver: webdriver.Chrome, base_url: str, account: Account) -> None:
    login_url = urljoin(base_url + "/", LOGIN_PATH.lstrip("/"))
    driver.delete_all_cookies()

    for attempt in range(1, LOGIN_ATTEMPTS + 1):
        print(f"登录尝试 {attempt}/{LOGIN_ATTEMPTS}")
        browser_page(driver, login_url, pause=1.0)

        username_input = find_first(
            driver,
            (
                (By.NAME, "username"),
                (By.CSS_SELECTOR, "input[name='username']"),
                (By.CSS_SELECTOR, "input[type='text']"),
            ),
        )
        password_input = find_first(
            driver,
            ((By.NAME, "password"), (By.CSS_SELECTOR, "input[type='password']")),
        )
        if username_input is None or password_input is None:
            if driver_indicates_logged_in(driver):
                print("账号登录成功")
                return
            if attempt == LOGIN_ATTEMPTS:
                raise CheckinError("登录页结构已变化：没有找到账号或密码输入框")
            print("登录表单暂时不可用，保留当前会话后重试")
            time.sleep(2)
            continue

        username_input.clear()
        username_input.send_keys(account.username)
        password_input.clear()
        password_input.send_keys(account.password)

        try:
            solve_captcha(driver)
        except CheckinError as exc:
            if attempt == LOGIN_ATTEMPTS:
                raise
            print(f"{exc}，刷新后重试")
            continue

        submit = find_first(
            driver,
            (
                (By.CSS_SELECTOR, "button[name='loginsubmit']"),
                (By.CSS_SELECTOR, "input[name='loginsubmit']"),
                (By.CSS_SELECTOR, "button[type='submit']"),
                (By.CSS_SELECTOR, "input[type='submit']"),
            ),
        )
        if submit is None:
            raise CheckinError("登录页结构已变化：没有找到登录按钮")

        submit.click()
        time.sleep(3)
        page_text = driver.page_source
        if driver_indicates_logged_in(driver):
            print("账号登录成功")
            return
        if any(marker in page_text for marker in LOGIN_FAILURE_MARKERS):
            if attempt == LOGIN_ATTEMPTS:
                raise CheckinError("登录失败，请检查账号、密码或验证码")
            print("登录未成功，保留当前会话并刷新验证码后重试")
            continue

        # 重新访问个人页，避免 Discuz 弹窗登录完成后页面状态没有及时刷新。
        browser_page(
            driver,
            urljoin(base_url + "/", "home.php?mod=space"),
            pause=1.5,
        )
        if driver_indicates_logged_in(driver):
            print("账号登录成功")
            return

    raise CheckinError("登录状态验证失败")


def perform_checkin(driver: webdriver.Chrome, base_url: str) -> str:
    sign_url = urljoin(base_url + "/", SIGN_PATH.lstrip("/"))
    print("正在打开签到页面")
    browser_page(driver, sign_url, pause=1.5)
    page_text = driver.page_source

    if any(marker in page_text for marker in SIGNED_MARKERS):
        return "今日已签到"

    try:
        sign_button = WebDriverWait(driver, 15).until(
            EC.element_to_be_clickable((By.ID, "JD_sign"))
        )
    except TimeoutException as exc:
        raise CheckinError("签到页面结构已变化：没有找到签到按钮") from exc

    sign_button.click()
    try:
        WebDriverWait(driver, 12).until(
            lambda current: any(
                marker in current.page_source
                for marker in (*SIGNED_MARKERS, *SUCCESS_MARKERS)
            )
        )
    except TimeoutException:
        driver.refresh()
        wait_for_document(driver)
        time.sleep(1.5)

    result_page = driver.page_source
    if any(marker in result_page for marker in SIGNED_MARKERS):
        return "签到成功（页面显示今日已签到）"
    if any(marker in result_page for marker in SUCCESS_MARKERS):
        return "签到成功"
    raise CheckinError("点击签到后未检测到成功状态")


def run() -> int:
    accounts = parse_accounts(os.getenv("XSIJISHE", ""))
    print(f"已读取 {len(accounts)} 个账号")

    with managed_webdriver() as discovery_driver:
        base_urls = resolve_forum_urls(discovery_driver)

    failures: list[str] = []
    for index, account in enumerate(accounts, start=1):
        masked = mask_username(account.username)
        print(f"开始处理账号 {index}/{len(accounts)}：{masked}")
        account_errors: list[str] = []
        for address_index, base_url in enumerate(base_urls, start=1):
            host = urlparse(base_url).hostname or base_url
            print(f"使用有效地址 {address_index}/{len(base_urls)} 处理账号：{host}")
            try:
                with managed_webdriver() as driver:
                    login(driver, base_url, account)
                    result = perform_checkin(driver, base_url)
                print(f"账号 {masked}：{result}")
                break
            except CheckinError as exc:
                account_errors.append(f"{host}：{exc}")
                print(f"地址 {host} 处理失败：{exc}", file=sys.stderr)
            except Exception as exc:
                account_errors.append(f"{host}：未预期错误 {type(exc).__name__}")
                print(
                    f"地址 {host} 发生未预期错误：{type(exc).__name__}",
                    file=sys.stderr,
                )
        else:
            summary = "；".join(account_errors)
            failures.append(f"账号 {masked}：所有有效地址均失败（{summary}）")

    if failures:
        print("签到任务失败：", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1

    print("所有账号处理完成")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(run())
    except CheckinError as exc:
        print(f"任务失败：{exc}", file=sys.stderr)
        raise SystemExit(1)
