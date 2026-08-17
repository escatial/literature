#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
超级鹰验证码识别平台 —— 完整集成模块
======================================

依据官方开发文档（https://www.chaojiying.com/api-5.html）实现三大核心接口：

| 接口         | 地址                        | 用途                                 |
|--------------|-----------------------------|--------------------------------------|
| 识别图像     | /Upload/Processing.php      | 上传图片返回识别结果（上传即扣费）   |
| 报错返分     | /Upload/ReportError.php     | 识别结果错误时返还题分（3 分钟内）   |
| 查询信息     | /Upload/GetScore.php        | 查询账户题分余额                     |

错误码对照表：https://www.chaojiying.com/api-23.html
验证码类型价格表：https://www.chaojiying.com/price.html

特性：
  - md5 密码鉴权（pass2 字段）
  - 网络异常自动重试（指数退避）
  - 错误码分类兜底：可重试 / 致命 / 跳过
  - 全场景调度：滑块(9902→9900→9602)、英数(1005→1902→1004)、点选(9004→9006→9101) 自动降级

注：此文件为嵌入项目内的副本（源：中国知网/cjy_client.py，已随嵌入删除）。
"""
import logging
import time

import requests
from hashlib import md5

logger = logging.getLogger("cjy")

# ========================== 常量 ==========================
API_HOST = "https://upload.chaojiying.net"
URL_RECOGNIZE = f"{API_HOST}/Upload/Processing.php"
URL_REPORT = f"{API_HOST}/Upload/ReportError.php"
URL_SCORE = f"{API_HOST}/Upload/GetScore.php"

# 本项目用到的验证码类型（完整列表见 price.html）
CODETYPE = {
    "en_num_4": 1004,      # 1~4 位英文数字
    "en_num_5": 1005,      # 1~5 位英文数字  ← 知网翻页 vericodeForm（5 位英数）
    "en_num_6": 1902,      # 4~6 位英文数字
    "en_num_8": 1008,      # 1~8 位英文数字
    "slider_2": 9902,      # 两个图形块中心点坐标  ← 知网滑块验证码
    "slider_gap": 9900,    # 滑块/缺口/色块定位
    "slider_h": 9602,      # 水平拼图两坐标（x 相减取绝对值）
    "point_1": 9101,       # 固定 1 个坐标（点选）
    "point_4": 9004,       # 通用 1~4 个坐标
    "point_6": 9006,       # 通用 1~6 个坐标
}

# 场景 → 识别类型降级链（按顺序尝试，失败自动降级）
STRATEGY = {
    "slider": [9902, 9900, 9602],
    "alnum": [1005, 1902, 1004],
    "point": [9004, 9006, 9101],
}

# 错误码分类（api-23.html）
ERR_OK = 0
# 可重试：参数错误/上传问题/限流
ERR_RETRYABLE = {-429, -1004, -10061, -100612, -10062, -2001}
# 跳过：图片无法识别 → 换下一张
ERR_SKIP = {-10064}
# 致命：账号/密码/余额/资源包/IP 受限/错误率过高，重试无意义
ERR_FATAL = {-1001, -1002, -10023, -1005, -10052, -10071, -10072, -1013}


class CjyError(Exception):
    """超级鹰业务异常（带错误码）"""

    def __init__(self, err_no: int, err_str: str, pic_id: str = None):
        self.err_no = err_no
        self.err_str = err_str
        self.pic_id = pic_id
        super().__init__(f"[超级鹰 {err_no}] {err_str}")


class CjyFatalError(CjyError):
    """致命错误：账号/余额/IP 受限等，重试无意义"""


# ========================== 结果解析 ==========================
def parse_pic_str(codetype, pic_str):
    """
    把超级鹰返回的 pic_str 解析为结构化结果。
    坐标类返回 (points, "")，文本类返回 ([], text)。
    """
    if codetype in (9902, 9900, 9602, 9004, 9006, 9101):
        points = []
        for p in str(pic_str or "").split("|"):
            parts = p.split(",")
            if len(parts) >= 2:
                x, y = parts[0].strip(), parts[1].strip()
                if x.lstrip("-").isdigit() and y.lstrip("-").isdigit():
                    points.append((int(x), int(y)))
        return points, ""
    return [], str(pic_str or "").strip()


class RecognizeResult:
    """统一识别结果"""

    def __init__(self, codetype: int, pic_id: str, pic_str: str, raw: dict):
        self.codetype = codetype
        self.pic_id = pic_id
        self.pic_str = pic_str
        self.raw = raw
        self.points, self.text = parse_pic_str(codetype, pic_str)

    def __repr__(self):
        return f"<RecognizeResult type={self.codetype} pic_str={self.pic_str!r}>"


# ========================== 客户端 ==========================
class CjyClient:
    """超级鹰三大核心接口的完整封装：参数组装 + 鉴权 + 重试 + 解析"""

    def __init__(self, user: str, passwd: str, soft_id: str = "",
                 timeout: int = 60, retry_times: int = 3,
                 backoff: float = 1.0, max_backoff: float = 8.0):
        self.user = user
        self.soft_id = soft_id
        self.timeout = timeout
        self.retry_times = retry_times
        self.backoff = backoff
        self.max_backoff = max_backoff
        # 官方推荐：pass2 = md5(密码) 32 位小写
        self.base_params = {
            "user": user,
            "pass2": md5(passwd.encode("utf-8")).hexdigest(),
            "softid": soft_id,
        }
        self.headers = {
            "User-Agent": "Mozilla/4.0 (compatible; MSIE 8.0; Windows NT 5.1; Trident/4.0)",
            "Connection": "Keep-Alive",
        }

    # ---------- 网络层：指数退避重试 ----------
    def _post(self, url: str, params: dict, files: dict = None,
              retry_times: int = None) -> dict:
        """POST + 网络异常/HTTP错误/解析失败 自动重试，返回 JSON dict"""
        times = retry_times if retry_times is not None else self.retry_times
        last_err = None
        for attempt in range(1, times + 1):
            try:
                resp = requests.post(url, data=params, files=files,
                                     headers=self.headers, timeout=self.timeout)
                resp.raise_for_status()
                return resp.json()
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                last_err = CjyError(-1, f"网络异常: {e}")
            except requests.exceptions.HTTPError as e:
                last_err = CjyError(-1, f"HTTP 错误({resp.status_code}): {e}")
            except ValueError as e:
                last_err = CjyError(-1, f"响应 JSON 解析失败: {e}")
            logger.warning("%s（第 %d/%d 次）", last_err, attempt, times)
            if attempt < times:
                wait = min(self.backoff * (2 ** (attempt - 1)), self.max_backoff)
                time.sleep(wait)
        raise last_err if last_err else CjyError(-1, "未知网络错误")

    # ---------- 三大核心接口 ----------
    def recognize(self, im: bytes, codetype: int, str_debug: str = None) -> dict:
        """图像识别（上传即扣费）。返回 {err_no, err_str, pic_id, pic_str, md5}"""
        params = {"codetype": codetype}
        if str_debug:
            params["str_debug"] = str_debug
        params.update(self.base_params)
        data = self._post(URL_RECOGNIZE, params, files={"userfile": ("ccc.jpg", im)})
        self._raise_if_error(data)
        return data

    def recognize_base64(self, b64_str: str, codetype: int, str_debug: str = None) -> dict:
        """base64 方式识别"""
        params = {"codetype": codetype, "file_base64": b64_str}
        if str_debug:
            params["str_debug"] = str_debug
        params.update(self.base_params)
        data = self._post(URL_RECOGNIZE, params)
        self._raise_if_error(data)
        return data

    def report_error(self, pic_id: str) -> dict:
        """报错返分：识别结果错误时调用（3 分钟内有效，不可乱调）"""
        params = {"id": pic_id}
        params.update(self.base_params)
        return self._post(URL_REPORT, params)

    def get_score(self) -> dict:
        """查询余额：{err_no, err_str, tifen, tifen_lock}"""
        return self._post(URL_SCORE, {
            "user": self.user,
            "pass2": self.base_params["pass2"],
        })

    # ---------- 错误兜底 ----------
    @staticmethod
    def _raise_if_error(data: dict):
        """err_no != 0 时按错误码分类抛异常"""
        err_no = data.get("err_no")
        if err_no == ERR_OK:
            return
        err_str = data.get("err_str", "")
        pic_id = data.get("pic_id")
        if err_no in ERR_FATAL:
            raise CjyFatalError(err_no, err_str, pic_id)
        raise CjyError(err_no, err_str, pic_id)


# ========================== 全场景调度器 ==========================
class CaptchaDispatcher:
    """验证码自动识别调度器：按场景选型 + 自动降级 + 统计"""

    def __init__(self, client: CjyClient, strategy: dict = None):
        self.client = client
        self.strategy = {**STRATEGY, **(strategy or {})}
        self.stats = {"ok": 0, "fail": 0, "fallback": 0, "retry": 0, "empty": 0, "skip": 0}

    def recognize(self, im: bytes, scene: str, codetypes: list = None,
                  retry_times: int = 1, require: str = None,
                  min_points: int = None) -> RecognizeResult:
        """
        统一识别入口。
        :param im:          图片字节
        :param scene:       场景关键字 slider / alnum / point
        :param codetypes:   覆盖默认降级链（如 [1005]）
        :param retry_times: 单个类型内的重试次数
        :param require:     期望结果类型 "points" / "text"，不满足则降级换类型
        :param min_points:  require="points" 时的最少坐标数（如滑块需 ≥2），
                            不足视为识别失败进入降级链（解决 9900 只返回 1 点的问题）
        """
        chain = codetypes or self.strategy.get(scene) or [scene]
        last_err = None
        for idx, ct in enumerate(chain):
            for attempt in range(1, max(retry_times, 1) + 1):
                try:
                    data = self.client.recognize(im, ct)
                except CjyFatalError as e:
                    # 致命错误直接抛出，不再重试/降级
                    self.stats["fail"] += 1
                    raise
                except CjyError as e:
                    self.stats["fail"] += 1
                    last_err = e
                    if e.err_no in ERR_RETRYABLE and attempt < max(retry_times, 1):
                        self.stats["retry"] += 1
                        time.sleep(1.0)
                        continue
                    if e.err_no in ERR_SKIP:
                        # 图片无法识别（如 -10064）：重试无意义，直接换类型
                        self.stats["skip"] += 1
                        break
                    break  # 当前类型放弃，进入降级链下一个
                result = RecognizeResult(ct, data.get("pic_id"), data.get("pic_str"), data)
                if require == "points":
                    n_points = len(result.points)
                    if not n_points or (min_points and n_points < min_points):
                        self.stats["empty"] += 1
                        last_err = CjyError(0, f"类型 {ct} 坐标不足{min_points or 1}: {result.pic_str!r}")
                        break
                if require == "text" and not result.text:
                    self.stats["empty"] += 1
                    last_err = CjyError(0, f"类型 {ct} 返回文本为空: {result.pic_str!r}")
                    break
                self.stats["ok"] += 1
                return result
            if idx < len(chain) - 1:
                self.stats["fallback"] += 1
                print(f"[超级鹰] {scene} 类型 {ct} 失败，降级尝试 {chain[idx + 1]}")
        raise CjyError(last_err.err_no if last_err else -1,
                       f"{scene} 场景识别全部失败: {last_err.err_str if last_err else '无可用策略'}")


# ========================== 自测 ==========================
if __name__ == "__main__":
    import os
    user = os.environ.get("CNKI_CJY_USER") or os.environ.get("CJY_USER", "")
    passwd = os.environ.get("CNKI_CJY_PASS") or os.environ.get("CJY_PASS", "")
    soft = os.environ.get("CNKI_CJY_SOFT_ID") or os.environ.get("CJY_SOFT_ID", "")
    if not all((user, passwd, soft)):
        raise RuntimeError("缺少超级鹰环境变量")
    client = CjyClient(user, passwd, soft)
    print("[余额]", client.get_score())
