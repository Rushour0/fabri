/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Comma separated Studio sections to show, e.g. "setups,settings". Unset shows all. */
  readonly VITE_STUDIO_SECTIONS?: string;
}
