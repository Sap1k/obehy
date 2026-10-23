import { act, render } from "@testing-library/react";
import { follow, resetMapStore, setPanelsRight, unfollow, useMapStore } from "./store";

describe("map store", () => {
  beforeEach(resetMapStore);

  it("re-renders a component for its own slice only", () => {
    let renders = 0;
    function Followed() {
      renders += 1;
      return <span>{useMapStore((s) => s.followedId) ?? "none"}</span>;
    }
    const { container } = render(<Followed />);
    expect(renders).toBe(1);
    act(() => setPanelsRight(470));
    expect(renders).toBe(1);
    act(() => follow("v1"));
    expect(container.textContent).toBe("v1");
    expect(renders).toBe(2);
    act(() => follow("v1"));
    expect(renders).toBe(2);
    act(() => unfollow());
    expect(container.textContent).toBe("none");
  });
});
