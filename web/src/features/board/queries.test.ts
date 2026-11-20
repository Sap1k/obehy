import { afterEach, describe, expect, it, vi } from "vitest";
import { api, type Departure } from "../../api/client";
import { boardRowKey, fetchBoard } from "./queries";

const stop = {
  location_id: "jdf:stop:1",
  name: "Děčín hl.n.",
  kind: "stop_place",
  domain: "rail",
  municipality: null,
  district: null,
  latitude: null,
  longitude: null,
  coordinate_precision: "exact",
};

function answer(rows: number, next: string | null) {
  const departures = Array.from({ length: rows }, (_, i) => ({ trip_id: `t${i}` }));
  return {
    data: {
      release_id: "run",
      generated_at: "2026-10-22T12:00:00+02:00",
      stop,
      stops: [stop],
      since: "2026-10-22T10:30:00+02:00",
      until: "2026-10-22T13:30:00+02:00",
      departures,
      next,
      alerts: [{ alert_id: "stop" }, { alert_id: "line" }],
      stop_alerts: ["stop"],
    },
    response: new Response(null, { status: 200 }),
  };
}

/** The API, answering one page. */
function serve(page: ReturnType<typeof answer>) {
  return vi.spyOn(api, "GET").mockImplementation((() => Promise.resolve(page)) as never);
}

const scope = {
  stopId: "jdf:stop:1",
  only: undefined,
  arrivals: false,
  from: undefined,
  limit: 40,
};

describe("fetchBoard", () => {
  afterEach(() => vi.restoreAllMocks());

  it("asks once; the API fills the page", async () => {
    const get = serve(answer(40, "cursor"));
    const board = await fetchBoard(scope, new AbortController().signal);
    expect(get).toHaveBeenCalledTimes(1);
    expect(board.rows).toHaveLength(40);
    expect(board.next).toBe("cursor");
    // The stop's own alerts are its banners, as the API says.
    expect(board.stopAlerts.map((a) => a.alert_id)).toEqual(["stop"]);
  });

  it("asks for the stop's group, or for the one stop of it chosen", async () => {
    const get = serve(answer(40, "cursor"));
    const asked = () =>
      get.mock.lastCall?.[1] as unknown as { params: { path: unknown; query: unknown } };
    await fetchBoard(scope, new AbortController().signal);
    expect(asked().params.path).toEqual({ location_id: "jdf:stop:1" });
    expect(asked().params.query).toMatchObject({ group: true });
    await fetchBoard({ ...scope, only: "czptt:location:1" }, new AbortController().signal);
    expect(asked().params.path).toEqual({ location_id: "czptt:location:1" });
    expect(asked().params.query).toMatchObject({ group: false });
  });
});

describe("boardRowKey", () => {
  it("tells apart two calls of one trip at the stop place", () => {
    // Tram 5 at Nádraží Holešovice: post A, then post H a minute later.
    const call = {
      trip_id: "overlay:pid-gtfs:trip:ca29d0d32a7f3df4d856",
      service_date: "2026-10-25",
      location_id: "jdf:stop:10272",
    };
    const rows = [
      { ...call, sequence: 7, platform: "A" },
      { ...call, sequence: 8, platform: "H" },
    ] as Departure[];
    expect(new Set(rows.map(boardRowKey)).size).toBe(2);
  });
});
