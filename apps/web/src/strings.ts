/**
 * The dashboard's labels for the contract's closed lists. Each record is typed
 * by the generated union it labels, so a value added to the API's schema does
 * not compile here until it has its label.
 */
import type { LiveSessionStatus, Platform } from "@livecart/api-client";

import type { Tone } from "./ui/feedback";

export const PLATFORM_LABELS: Record<Platform, string> = {
  simulator: "Simulator",
  facebook: "Facebook",
  instagram: "Instagram",
  tiktok: "TikTok",
};

export const SESSION_STATUS: Record<LiveSessionStatus, { label: string; tone: Tone }> = {
  draft: { label: "Draft", tone: "neutral" },
  live: { label: "Live", tone: "danger" },
  ended: { label: "Ended", tone: "info" },
};
