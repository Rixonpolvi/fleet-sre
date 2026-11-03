"""D8 acceptance: fleet grid (d8-pins 12, Rows T1)."""

from d8_helpers import drive, make_client, make_host


def test_d8_t1_fleet_grid():
    """D8-T1: /api/fleet covers every armed deployment with evaluator health.

    After start(), every registry deployment is listed with every component
    and tripwire, each status equal to evaluator.status(); the HTML holds
    every deployment ID. Driven to F1's first trip, alpha's connector and
    deployment are tripped with the act's incident, and the rest are ok.
    """
    from fleetsre.control.incidents import IncidentState

    host = make_host("F1")
    rt = host.current
    client = make_client(host)

    response = client.get("/api/fleet")
    assert response.status_code == 200
    fleet = response.json()
    assert fleet["act"] == "F1"
    expected_ids = [d.id for d in rt.evaluator.deployments]
    got_ids = [
        d["id"] for service in fleet["services"] for d in service["deployments"]
    ]
    assert got_ids == expected_ids

    status = rt.evaluator.status()
    contract = rt.registry.contract("alert-ingestion")
    by_component: dict[str, list[str]] = {}
    for tripwire in contract.tripwires:
        by_component.setdefault(tripwire.component, []).append(tripwire.id)
    kinds = {c.id: c.kind for c in contract.components}
    for service in fleet["services"]:
        for deployment in service["deployments"]:
            assert deployment["incident"] is None
            assert deployment["health"] == "ok"
            got_components = {
                c["id"]: c for c in deployment["components"]
            }
            assert set(got_components) == set(by_component)
            for comp_id, comp in got_components.items():
                assert comp["kind"] == kinds[comp_id]
                assert comp["health"] == "ok"
                got_tw = {t["id"]: t["status"] for t in comp["tripwires"]}
                assert set(got_tw) == set(by_component[comp_id])
                for tw_id, tw_status in got_tw.items():
                    assert tw_status == status[deployment["id"]][tw_id]

    page = client.get("/")
    assert page.status_code == 200
    for deployment_id in expected_ids:
        assert deployment_id in page.text

    drive(rt, IncidentState.AWAITING_APPROVAL)
    fleet = client.get("/api/fleet").json()
    by_id = {
        d["id"]: d
        for service in fleet["services"]
        for d in service["deployments"]
    }
    alpha = by_id["alert-ingestion/alpha"]
    assert alpha["health"] == "tripped"
    connector = next(
        c for c in alpha["components"] if c["id"] == "connector"
    )
    assert connector["health"] == "tripped"
    assert alpha["incident"] is not None
    assert alpha["incident"]["id"] == rt.act.incident
    assert alpha["incident"]["state"] == "AWAITING_APPROVAL"
    for deployment_id, deployment in by_id.items():
        if deployment_id != "alert-ingestion/alpha":
            assert deployment["health"] == "ok", deployment_id
            assert deployment["incident"] is None, deployment_id
