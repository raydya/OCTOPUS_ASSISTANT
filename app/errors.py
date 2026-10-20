class AppError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        self.code = code
        self.message = message
        self.status = status
        super().__init__(message)


class FeishuError(AppError):
    def __init__(self, provider_code: str, status: int = 502):
        self.provider_code = provider_code
        # Do not echo upstream messages: some contain request data or credentials.
        message = "飞书请求失败，请检查应用权限、用户授权及应用可用范围。"
        if provider_code in {"99991672", "99991679", "99991678"}:
            message = "飞书权限不足。请在开放平台开通对应用户权限、发布应用，再重新授权。"
        elif provider_code in {"99991663", "99991668", "401", "invalid_grant"}:
            message = "飞书授权已失效，请重新连接账号。"
        elif provider_code in {"429", "99991400"}:
            message = "飞书请求频率受限，请稍后重试。已完成的同步页会保留。"
        elif provider_code == "network":
            message = "暂时无法连接飞书，请检查网络后重试。"
        super().__init__(f"feishu_{provider_code}", message, status)
