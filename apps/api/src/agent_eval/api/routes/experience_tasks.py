from fastapi import APIRouter, Response

from agent_eval.api.dependencies import WorkspaceSession
from agent_eval.api.routes.workspace import workspace_error
from agent_eval.api.schemas.experience_tasks import WorkspaceExperienceTaskResponse

router = APIRouter(prefix="/api/v1/workspace/experience-tasks", tags=["experience-tasks"])

EXPERIENCE_TASKS = (
    WorkspaceExperienceTaskResponse(
        slug="policy-tradeoff-analysis",
        title="分析公共政策方案的取舍",
        summary="形成一份区分事实、假设与价值判断的决策备忘录。",
        prompt=(
            "某城市只能选择一项投入：新增 40 辆电动公交车，或在中心城区开展拥堵收费试点。"
            "请从出行效率、公平性、成本和实施难度四个方面分析取舍，明确写出假设与缺失证据，"
            "不要编造本地统计数据，并以有条件的建议结尾。"
        ),
        category="analysis",
        difficulty="intermediate",
        recommended_mode="comparison",
        min_agents=2,
        max_agents=4,
    ),
    WorkspaceExperienceTaskResponse(
        slug="public-source-brief",
        title="使用可验证引用撰写研究简报",
        summary="只依据公开的一手来源形成简洁、可核查的研究说明。",
        prompt=(
            "请使用当前公开来源说明 IPCC 与 UNFCCC 的职责差异。每个组织至少引用一个官方来源，"
            "在关键事实后直接附上来源 URL，并区分来源事实与自己的综合判断。若无法浏览网页，"
            "请明确说明，不要伪造引用。"
        ),
        category="retrieval_citations",
        difficulty="intermediate",
        recommended_mode="comparison",
        min_agents=2,
        max_agents=5,
    ),
    WorkspaceExperienceTaskResponse(
        slug="incident-record-extraction",
        title="提取结构化事故记录",
        summary="把一段事故叙述转换成字段严格、可以审计的 JSON。",
        prompt=(
            "请把下面的叙述提取为 JSON，且只能包含这些键：incident_id、started_at、resolved_at、"
            "affected_services、root_cause、customer_impact。未说明的值使用 null，"
            "affected_services 使用数组，不要推断原文没有的事实。\n\n"
            "叙述：事故 INC-204 于 5 月 3 日 09:14 UTC 开始。部分欧洲客户无法使用结账和订单历史。"
            "团队在 10:02 UTC 回滚了错误配置的缓存规则，两个服务随即恢复。"
        ),
        category="structured_extraction",
        difficulty="beginner",
        recommended_mode="chat",
        min_agents=1,
        max_agents=3,
    ),
    WorkspaceExperienceTaskResponse(
        slug="constrained-customer-update",
        title="按精确约束撰写客户通知",
        summary="在格式、长度和语气约束下完成可直接发布的服务恢复通知。",
        prompt=(
            "请用中文撰写服务恢复通知，必须正好三段，不超过 220 个汉字，语气冷静并体现责任。"
            "不得使用“史无前例”或“小问题”，必须写明恢复时间 10:02 UTC，并以这句原文结尾："
            "我们将在五个工作日内发布完整复盘。"
        ),
        category="writing_instruction_following",
        difficulty="intermediate",
        recommended_mode="comparison",
        min_agents=2,
        max_agents=4,
    ),
    WorkspaceExperienceTaskResponse(
        slug="subscription-cohort-reasoning",
        title="根据渠道队列表进行数据推理",
        summary="计算可比较的比例，并说明现有数据无法证明什么。",
        prompt=(
            "某订阅产品试用数据如下：\n"
            "渠道 | 试用人数 | 付费转化 | 90 天留存\n"
            "搜索 | 1,200 | 300 | 210\n"
            "合作伙伴 | 500 | 175 | 105\n"
            "活动 | 240 | 96 | 72\n\n"
            "请分别计算转化率、转化用户中的留存率、每名试用用户对应的留存人数，"
            "按最后一项排序并展示公式；再说明仅凭此表不能证明排名第一渠道导致更高留存的两个原因。"
        ),
        category="data_reasoning",
        difficulty="intermediate",
        recommended_mode="comparison",
        min_agents=2,
        max_agents=5,
    ),
    WorkspaceExperienceTaskResponse(
        slug="multi-document-synthesis",
        title="综合存在冲突的项目记录",
        summary="汇总多份内部记录，同时保留不确定性、分歧和来源归属。",
        prompt=(
            "请把以下三份记录综合为管理层简报，使用“一致意见、尚存冲突、风险、下一步决策”四个部分。"
            "保留不确定性，并注明每项冲突来自哪份记录。\n\n"
            "产品记录：Beta 应在 8 月 12 日开放；引导流程已准备好，但导出功能尚未完成。\n"
            "工程记录：如果把压力测试移到下个迭代，导出功能可在 8 月 19 日完成；"
            "团队不建议这样取舍。\n"
            "支持记录：10 家设计合作伙伴可以接受一周内没有导出，但需要书面替代方案，"
            "并在开放访问前至少 48 小时收到通知。"
        ),
        category="long_context_synthesis",
        difficulty="advanced",
        recommended_mode="comparison",
        min_agents=2,
        max_agents=5,
    ),
)
TASKS_BY_SLUG = {task.slug: task for task in EXPERIENCE_TASKS}


@router.get("", response_model=list[WorkspaceExperienceTaskResponse])
async def list_experience_tasks(
    response: Response,
    _authorization: WorkspaceSession,
) -> tuple[WorkspaceExperienceTaskResponse, ...]:
    response.headers["Cache-Control"] = "private, no-store"
    return EXPERIENCE_TASKS


@router.get("/{slug}", response_model=WorkspaceExperienceTaskResponse)
async def get_experience_task(
    slug: str,
    response: Response,
    _authorization: WorkspaceSession,
) -> WorkspaceExperienceTaskResponse:
    task = TASKS_BY_SLUG.get(slug)
    if task is None:
        raise workspace_error(404, "experience_task_not_found", "Experience task not found")
    response.headers["Cache-Control"] = "private, no-store"
    return task
