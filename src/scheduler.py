"""处置调度：库位占用与释放的编排承接。

处置能不能做（状态、角色、字段）由 ``rules`` 判定；库位是否已登记、是否被占
由 repository 里的库位记录负责；本模块只负责把两者在一个事务内串起来：
批次转处置时占格，处置完成或复检转回时释放。
"""


# 释放库位的动作及默认原因；页面也可以传入更具体的原因
RELEASE_REASONS = {
    "destroy": "处置完成，批次已销毁，库位腾空",
    "recheck": "复检通过，转回普通检疫，库位腾空",
}


class LocationScheduler:
    def __init__(self, repository):
        self.repository = repository

    def register_location(self, actor, payload):
        return self.repository.register_location(
            code=payload["code"],
            name=str(payload.get("name") or ""),
            zone=str(payload.get("zone") or ""),
            actor_id=actor.user_id,
        )

    def list_locations(self):
        return self.repository.list_locations()

    def list_events(self):
        return self.repository.list_location_events()

    def open_occupancy(self, consignment_id):
        return self.repository.get_open_occupancy(consignment_id=consignment_id)

    def occupy(self, connection, entity, location_code, disposal_method, actor):
        """安排批次入格。同批次已有占用记录时沿用原单，返回 created=False。"""
        created, occupancy = self.repository.occupy_location(
            connection,
            location_code=location_code,
            consignment_id=entity["id"],
            consignment_code=entity["data"].get("code", ""),
            disposal_method=disposal_method,
            actor=actor,
        )
        return created, occupancy

    def default_reason(self, action):
        return RELEASE_REASONS.get(action, "库位释放")

    def release(self, connection, entity, action, reason, actor):
        reason = str(reason or "").strip() or self.default_reason(action)
        occupancy = self.repository.release_location(
            connection, entity["id"], reason, actor
        )
        return occupancy, reason
