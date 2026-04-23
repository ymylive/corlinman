import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import { CharacterCard, tiltForName, deriveTags } from "./character-card";
import type { AgentCard } from "@/lib/mocks/characters";

function mockMatchMedia(reduceMatches: boolean) {
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches:
      query === "(prefers-reduced-motion: reduce)" ? reduceMatches : false,
    media: query,
    onchange: null,
    addEventListener: () => void 0,
    removeEventListener: () => void 0,
    addListener: () => void 0,
    removeListener: () => void 0,
    dispatchEvent: () => false,
  })) as typeof window.matchMedia;
}

const SAMPLE: AgentCard = {
  name: "Mentor",
  emoji: "🧑‍🏫",
  description: "A senior developer who reviews your code.",
  system_prompt: "You are {{agent.mentor}}.",
  variables: { tone: "encouraging" },
  tools_allowed: ["read_file", "search_code", "run_tests"],
  skill_refs: [],
  source_path: "Agent/Mentor.md",
};

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("CharacterCard", () => {
  it("renders the agent name and description", () => {
    mockMatchMedia(false);
    render(<CharacterCard card={SAMPLE} onOpen={() => {}} />);
    const card = screen.getByTestId("character-card-back-Mentor");
    expect(card).toHaveTextContent("Mentor");
    expect(card).toHaveTextContent("A senior developer who reviews your code.");
  });

  it("fires onOpen when the card body is clicked", () => {
    mockMatchMedia(false);
    const onOpen = vi.fn();
    render(<CharacterCard card={SAMPLE} onOpen={onOpen} />);
    fireEvent.click(screen.getByTestId("character-card-back-Mentor"));
    expect(onOpen).toHaveBeenCalledTimes(1);
  });

  it("fires onOpen on Enter / Space when the card has focus", () => {
    mockMatchMedia(false);
    const onOpen = vi.fn();
    render(<CharacterCard card={SAMPLE} onOpen={onOpen} />);
    const card = screen.getByTestId("character-card-back-Mentor");
    fireEvent.keyDown(card, { key: "Enter" });
    fireEvent.keyDown(card, { key: " " });
    expect(onOpen).toHaveBeenCalledTimes(2);
  });

  it("renders the first 3 derived tag chips", () => {
    mockMatchMedia(false);
    render(<CharacterCard card={SAMPLE} onOpen={() => {}} />);
    expect(
      screen.getByTestId("character-card-tag-Mentor-read_file"),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId("character-card-tag-Mentor-search_code"),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId("character-card-tag-Mentor-run_tests"),
    ).toBeInTheDocument();
  });

  it("fires onEdit (not onOpen) when the Edit affordance is clicked", () => {
    mockMatchMedia(false);
    const onOpen = vi.fn();
    const onEdit = vi.fn();
    render(<CharacterCard card={SAMPLE} onOpen={onOpen} onEdit={onEdit} />);
    fireEvent.click(screen.getByTestId("character-card-edit-Mentor"));
    expect(onEdit).toHaveBeenCalledTimes(1);
    expect(onOpen).not.toHaveBeenCalled();
  });

  it("falls back to onOpen when onEdit is omitted", () => {
    mockMatchMedia(false);
    const onOpen = vi.fn();
    render(<CharacterCard card={SAMPLE} onOpen={onOpen} />);
    fireEvent.click(screen.getByTestId("character-card-edit-Mentor"));
    expect(onOpen).toHaveBeenCalledTimes(1);
  });
});

describe("deriveTags", () => {
  it("returns at most 3 tools plus a `+N` overflow chip", () => {
    const many = {
      ...SAMPLE,
      tools_allowed: ["a", "b", "c", "d", "e"],
    };
    const tags = deriveTags(many);
    expect(tags).toEqual(["a", "b", "c", "+2"]);
  });

  it("returns [] for an empty tool list", () => {
    expect(deriveTags({ ...SAMPLE, tools_allowed: [] })).toEqual([]);
  });
});

describe("tiltForName", () => {
  it("returns a stable value in [-1, 1] for the same name", () => {
    const a = tiltForName("Mentor");
    const b = tiltForName("Mentor");
    expect(a).toBe(b);
    expect(a).toBeGreaterThanOrEqual(-1);
    expect(a).toBeLessThanOrEqual(1);
  });

  it("returns different values for different names", () => {
    const all = ["Mentor", "Researcher", "Critic", "DataSci"].map(tiltForName);
    const unique = new Set(all);
    expect(unique.size).toBeGreaterThan(1);
  });
});
