"""框架级异常。"""


class AgentAccountsError(Exception):
    pass


class AccountFrozen(AgentAccountsError):
    """账号总开关处于冻结状态，拒绝一切自动化操作。"""


class HumanRequired(AgentAccountsError):
    """需要人工介入：登录失效、验证码、风控提示等。系统只停下来通知人，不做任何绕过。"""

    def __init__(self, reason: str, detail: str = "", *, freeze: bool = False):
        super().__init__(f"{reason}{'：' + detail if detail else ''}")
        self.reason = reason
        self.detail = detail
        # 风控 / 验证码类：自动冻结账号，必须人工确认后 unfreeze 才能继续
        self.freeze = freeze
