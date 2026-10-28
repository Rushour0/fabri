import { createContext, useContext } from "react";
import { DEFAULT_UI, type UiConfig, type UiLabelKey } from "../../lib/setups";

// The setups config's `ui:` block, merged over Studio's defaults by SetupsView.
export const UiConfigContext = createContext<UiConfig>(DEFAULT_UI);

export function useUi(): UiConfig {
  return useContext(UiConfigContext);
}

export function useLabel(): (key: UiLabelKey) => string {
  const ui = useUi();
  return (key) => ui.labels[key];
}
