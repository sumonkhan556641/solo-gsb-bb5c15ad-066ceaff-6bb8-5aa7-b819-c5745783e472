"""FastAPI 应用与路由：食品换线放行 HTTP API。"""
from __future__ import annotations

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from . import config
from .db import Store, initialize
from .errors import APIError
from .schemas import (
    BatchCorrection,
    BatchIn,
    CleaningCorrection,
    MaterialLotCorrection,
    MaterialLotIn,
    VerificationIn,
)
from .service import Service


def create_app(db_path: str | None = None, seed_demo: bool | None = None) -> FastAPI:
    app = FastAPI(
        title="食品换线放行服务",
        description=(
            "面向共用产线的生产批次过敏原换线放行：登记原料批号与批次、"
            "计算残留传递与未声明过敏原，给出放行/扣留裁决；支持更正重算与历史版本查询。"
        ),
        version="1.0.0",
    )

    store = Store(db_path or config.DB_PATH)
    store.connect()
    initialize(store, config.SEED_DEMO_DATA if seed_demo is None else seed_demo)
    app.state.store = store
    app.state.service = Service(store)

    @app.exception_handler(APIError)
    async def handle_api_error(_: Request, exc: APIError) -> JSONResponse:
        error: dict = {"code": exc.code, "message": exc.message}
        if exc.details is not None:
            error["details"] = exc.details
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": error},
        )

    def svc() -> Service:
        return app.state.service

    @app.get("/health", tags=["系统"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    # ---------------- 过敏原字典 ----------------

    @app.get("/api/allergens", tags=["过敏原字典"])
    def list_allergens(service: Service = Depends(svc)):
        return {"allergens": service.list_allergens()}

    # ---------------- 原料批号 ----------------

    @app.post("/api/material-lots", tags=["原料批号"], status_code=201)
    def create_material_lot(body: MaterialLotIn, service: Service = Depends(svc)):
        return service.create_material_lot(body.lot_no, body.material, body.allergens)

    @app.get("/api/material-lots", tags=["原料批号"])
    def list_material_lots(service: Service = Depends(svc)):
        return {"material_lots": service.list_material_lots()}

    @app.get("/api/material-lots/{lot_no}", tags=["原料批号"])
    def get_material_lot(lot_no: str, service: Service = Depends(svc)):
        return service.get_material_lot(lot_no)

    @app.put("/api/material-lots/{lot_no}", tags=["原料批号"])
    def correct_material_lot(lot_no: str, body: MaterialLotCorrection, service: Service = Depends(svc)):
        """更正原料批号（名称/过敏原），重算受影响批次及后续批次。"""
        return service.correct_material_lot(lot_no, body.material, body.allergens)

    # ---------------- 产线 ----------------

    @app.get("/api/lines", tags=["产线"])
    def list_lines(service: Service = Depends(svc)):
        return {"lines": service.list_lines()}

    # ---------------- 批次与裁决 ----------------

    @app.post("/api/batches", tags=["批次"], status_code=201)
    def register_batch(body: BatchIn, service: Service = Depends(svc)):
        """按产线顺序登记一个生产批次，立即返回裁决。"""
        return service.register_batch(
            batch_no=body.batch_no,
            line_id=body.line_id,
            material_lots=body.material_lots,
            declared_allergens=body.declared_allergens,
            cleaned_allergens=body.cleaned_allergens,
        )

    @app.get("/api/batches", tags=["批次"])
    def list_batches(line_id: str | None = None, service: Service = Depends(svc)):
        return {"batches": service.list_batches(line_id)}

    @app.get("/api/batches/{batch_no}", tags=["批次"])
    def get_batch(batch_no: str, service: Service = Depends(svc)):
        return service.get_batch(batch_no)

    @app.put("/api/batches/{batch_no}", tags=["批次"])
    def correct_batch(batch_no: str, body: BatchCorrection, service: Service = Depends(svc)):
        """更正批次的原料批号/声明/清洁核验，按产线顺序重算本批及后续批次。"""
        return service.correct_batch(
            batch_no,
            body.material_lots,
            body.declared_allergens,
            body.cleaned_allergens,
        )

    @app.put("/api/batches/{batch_no}/cleaning", tags=["批次"])
    def correct_cleaning(batch_no: str, body: CleaningCorrection, service: Service = Depends(svc)):
        """专用：更正“批次开始前清洁核验确认清除的过敏原”。"""
        return service.correct_cleaning(batch_no, body.cleaned_allergens)

    @app.get("/api/batches/{batch_no}/history", tags=["批次"])
    def batch_history(batch_no: str, service: Service = Depends(svc)):
        """查询某批次历次裁决版本（可审计的历史裁决）。"""
        return service.batch_history(batch_no)

    # ---------------- 出货核验单 ----------------

    @app.post("/api/verifications", tags=["出货核验单"], status_code=201)
    def create_verification(body: VerificationIn, service: Service = Depends(svc)):
        """创建出货核验单（草稿）：唯一单号 + 非空、无重复的已登记批次号列表。

        逐批保存创建时的当前裁决版本与结论；未知批次、重复单号/批次均整体拒绝。
        """
        return service.create_verification(body.verification_no, body.batch_nos)

    @app.get("/api/verifications", tags=["出货核验单"])
    def list_verifications(status: str | None = None, service: Service = Depends(svc)):
        """列出出货核验单（可按 DRAFT / PENDING_REVIEW / CONFIRMED 过滤）。"""
        return service.list_verifications(status)

    @app.get("/api/verifications/{verification_no}", tags=["出货核验单"])
    def get_verification(verification_no: str, service: Service = Depends(svc)):
        """查询核验单状态、逐批记录（快照版本/当前版本/变化原因）与历次确认快照。"""
        return service.get_verification(verification_no)

    @app.post("/api/verifications/{verification_no}/reverify", tags=["出货核验单"])
    def reverify_verification(verification_no: str, service: Service = Depends(svc)):
        """重新核验：刷新逐批快照并回到草稿（草稿/待复核可用；已确认拒绝）。"""
        return service.reverify_verification(verification_no)

    @app.post("/api/verifications/{verification_no}/confirm", tags=["出货核验单"])
    def confirm_verification(verification_no: str, service: Service = Depends(svc)):
        """确认：原子校验逐批快照仍为当前版本且全部 RELEASE。

        不符时返回 422 与不符批次明细（STALE_SNAPSHOT / BATCH_ON_HOLD），单据保持未确认。
        """
        return service.confirm_verification(verification_no)

    return app


app = create_app()
