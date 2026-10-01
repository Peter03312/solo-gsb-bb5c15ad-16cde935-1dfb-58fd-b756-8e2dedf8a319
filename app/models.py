"""请求体模型。"""
from typing import Annotated, List, Literal, Optional, Union

from pydantic import BaseModel, Field


class StratumIn(BaseModel):
    stratum_id: str = Field(min_length=1, max_length=128, description="地层编号，全库唯一")
    site: Optional[str] = Field(default=None, max_length=256, description="遗址名称")


class AssertOp(BaseModel):
    """新增断言。relation=earlier_than 表示 stratum_a 早于 stratum_b。"""

    op: Literal["assert"]
    evidence_id: str = Field(min_length=1, max_length=128, description="证据编号，全库唯一（含已撤回）")
    stratum_a: str = Field(min_length=1, max_length=128)
    stratum_b: str = Field(min_length=1, max_length=128)
    relation: Literal["same_period", "earlier_than"]


class RetractOp(BaseModel):
    """撤回断言（按证据编号，仅可撤回生效中的断言）。"""

    op: Literal["retract"]
    evidence_id: str = Field(min_length=1, max_length=128)


Operation = Annotated[Union[AssertOp, RetractOp], Field(discriminator="op")]


class BatchIn(BaseModel):
    """一批断言变更。须携带当前修订号；整批按应用后的最终关系核验，
    任一冲突则整批拒绝、原状态不变；全部通过则原子递增修订号。"""

    expected_revision: int = Field(ge=0, description="当前修订号；不一致则整批拒绝")
    operations: List[Operation] = Field(min_length=1)


class PreflightIn(BaseModel):
    """提交前预检（只读，不落库、不递增修订号）：沿用正式批次的
    expected_revision / operations 语义，并指定允许额外撤回的生效旧证据数上限。

    服务在该修订号对应的同一正式视图中先按原批次语义处理新增与撤回；
    若处理后仍冲突，寻找不含本批新增证据、也不与本批撤回重复的最少额外撤回集合。
    """

    expected_revision: int = Field(ge=0, description="当前修订号；不一致则按 STALE_REVISION 拒绝")
    operations: List[Operation] = Field(min_length=1)
    extra_retraction_budget: int = Field(
        default=0,
        ge=0,
        le=3,
        description="可额外撤回的生效旧证据数上限（0～3，默认 0）",
    )


class SnapshotIn(BaseModel):
    label: Optional[str] = Field(default=None, max_length=256, description="版本备注")


class ProjectionIn(BaseModel):
    """创建推演：固定当前正式修订号、已登记地层与生效断言快照。"""

    label: Optional[str] = Field(default=None, max_length=256, description="推演备注")


class PackageIn(BaseModel):
    """专题成果包封存请求：指定定版编号与一组已登记地层（至少两个、不重复）。

    业务校验（空 / 不足两个 / 重复 / 定版不存在 / 地层未登记）在包服务层显式完成，
    并分别返回明确错误码；校验失败不留任何半成品。
    """

    version: int = Field(ge=1, description="定版编号")
    strata: List[str] = Field(description="已登记地层编号；至少两个且不重复（顺序不影响结果，自动排序去重）")


class ProjectionBatchIn(BaseModel):
    """推演内的一批断言变更。须携带该推演当前修订号（自 0 起，批次成功后递增）；
    语义与正式批次一致：整批按应用后的最终关系核验，任一冲突则整批拒绝、推演状态不变。"""

    expected_revision: int = Field(ge=0, description="该推演当前修订号；不一致则整批拒绝")
    operations: List[Operation] = Field(min_length=1)
