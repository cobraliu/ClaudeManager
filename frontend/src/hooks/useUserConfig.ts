import { useCallback, useEffect, useState } from "react";

export type LayoutScheme = "classic" | "chat-centric";

export interface UserConfig {
  layout: LayoutScheme;
  terminalOpen: boolean;
  terminalHeight: number;
  chatCentricRightWidth: number;
}

const STORAGE_KEY = "cm_user_config_v1";

const DEFAULT_CONFIG: UserConfig = {
  layout: "classic",
  terminalOpen: false,
  terminalHeight: 280,
  chatCentricRightWidth: 320,
};

function loadConfig(): UserConfig {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return DEFAULT_CONFIG;
    const parsed = JSON.parse(raw);
    const merged = { ...DEFAULT_CONFIG, ...parsed };
    // Migrate removed "focus" layout to classic
    if (merged.layout !== "classic" && merged.layout !== "chat-centric") {
      merged.layout = "classic";
    }
    return merged;
  } catch {
    return DEFAULT_CONFIG;
  }
}

function saveConfig(cfg: UserConfig) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(cfg));
  } catch {
    /* ignore */
  }
}

export function useUserConfig() {
  const [config, setConfig] = useState<UserConfig>(loadConfig);

  useEffect(() => {
    saveConfig(config);
  }, [config]);

  const update = useCallback(<K extends keyof UserConfig>(key: K, value: UserConfig[K]) => {
    setConfig(prev => ({ ...prev, [key]: value }));
  }, []);

  const patch = useCallback((partial: Partial<UserConfig>) => {
    setConfig(prev => ({ ...prev, ...partial }));
  }, []);

  return { config, update, patch, setConfig };
}
