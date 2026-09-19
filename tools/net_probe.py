"""连通性探针: 用 Python 自己的 ssl(OpenSSL) 测各条出网路径。

为什么不用 curl 诊断: 本机 Windows schannel 拿不到凭证, curl.exe 一律报
    curl: (35) schannel: AcquireCredentialsHandle failed: SEC_E_NO_CREDENTIALS
那是 schannel 的问题, 不代表网络不通。Python 的 ssl 走自带 OpenSSL, 才是
pip 真实使用的栈, 所以诊断 PyPI 连通性必须用它。

用法:
    .venv\\Scripts\\python.exe tools\\net_probe.py
    .venv\\Scripts\\python.exe tools\\net_probe.py --proxy http://127.0.0.1:10808
"""
import argparse
import time
import urllib.request

DEFAULT_URLS = {
    "pypi": "https://pypi.org/simple/mss/",
    "tuna": "https://pypi.tuna.tsinghua.edu.cn/simple/mss/",
    "aliyun": "https://mirrors.aliyun.com/pypi/simple/mss/",
    "github": "https://github.com/",
}


def probe(route_name, proxy, urls, timeout):
    for url_name, url in urls.items():
        if proxy is None:
            handler = urllib.request.ProxyHandler({})       # 空 dict = 明确不走代理
        else:
            handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        opener = urllib.request.build_opener(handler)
        t0 = time.time()
        try:
            resp = opener.open(url, timeout=timeout)
            resp.read(64)
            print("%-13s %-8s OK    %s  %5.1fs"
                  % (route_name, url_name, resp.status, time.time() - t0))
        except Exception as exc:  # noqa: BLE001
            print("%-13s %-8s FAIL  %-20s %5.1fs  %s"
                  % (route_name, url_name, type(exc).__name__,
                     time.time() - t0, str(exc)[:70]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy", default="http://127.0.0.1:10808")
    ap.add_argument("--timeout", type=float, default=12.0)
    args = ap.parse_args()

    routes = [("direct", None)]
    if args.proxy:
        routes.append(("proxy", args.proxy))
    for name, proxy in routes:
        probe(name, proxy, DEFAULT_URLS, args.timeout)


if __name__ == "__main__":
    main()
