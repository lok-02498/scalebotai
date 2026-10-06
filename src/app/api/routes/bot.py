from fastapi import APIRouter

from app.api.deps import BotDep, CurrentUser, SettingsDep
from app.schemas.chat import BotInfo
from app.schemas.common import error_responses

router = APIRouter(prefix="/api/v1/bot", tags=["Bot"], responses=error_responses(401, 429))


@router.get("/info", response_model=BotInfo, summary="Active chatbot engine and its status")
async def bot_info(_: CurrentUser, bot: BotDep, settings: SettingsDep) -> BotInfo:
    llm_available = await bot.is_available() if settings.chatbot_mode == "llm" else False
    return BotInfo(
        mode=settings.chatbot_mode,
        model=bot.model,
        intents=bot.intent_count,
        llm_available=llm_available,
    )
