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


class PeriodAnchorIn(BaseModel):
    """地层期位锚点：stratum 必须位于第 position 期（1～total_periods）。

    同一期等价组中的锚点须指同一期位，否则分析结果不可行（anchor_position_conflict）；
    锚点地层编号在请求内不得重复（重复属请求结构错误，明确拒绝）。
    """

    stratum: str = Field(min_length=1, max_length=128, description="已登记（或推演基线内）的地层编号")
    position: int = Field(ge=1, le=64, description="锚定期位（1～总期数，期位越界在服务层按总期数再校验）")


class ArrangementIn(BaseModel):
    """分期试排（只读，不写修订）：在正式或指定推演的**同一只读视图**内读取地层与生效断言。

    - view=formal（默认）：在 expected_revision 对应的当前正式视图中分析，过期按
      STALE_REVISION 拒绝；view=projection：按 projection_id 推演的冻结视图
      （已关闭推演仍按其冻结视图分析），expected_revision 须为该推演当前修订号；
    - total_periods 为总期数（1～64）；
    - anchors 为可选地层期位锚点；同一期等价组占同一期、早于源组期位严格小于目标组、
      未锚定组可占任一期；
    - 可行时返回每组所含地层及其在**所有**可行排布中的最早/最晚期位（不给单套任意排布）；
      不可行时区分同组锚点矛盾与先后链不相容（对期数或锚点），附关联锚点与证据路径，
      不返回部分排布。全程只读，不写库、不递增任何修订号。
    """

    view: Literal["formal", "projection"] = Field(
        default="formal", description="视图：formal=当前正式修订视图；projection=指定推演的冻结视图"
    )
    projection_id: Optional[int] = Field(
        default=None, ge=1, description="view=projection 时必填：推演编号（含已关闭推演的冻结视图）"
    )
    expected_revision: int = Field(
        ge=0, description="预期修订号：正式视图为当前正式修订号；推演视图为该推演当前修订号"
    )
    total_periods: int = Field(ge=1, le=64, description="总期数（1～64）")
    anchors: Optional[List[PeriodAnchorIn]] = Field(
        default=None, description="可选地层期位锚点；锚点地层编号不得重复，期位须在 1～总期数内"
    )
