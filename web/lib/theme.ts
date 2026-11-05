// The theme (#399): dark by default, light on the switch, remembered in this browser only.

export const THEME_KEY = "nhl-edge-theme";

/** Before the first paint, the theme this browser chose, so a light reader never sees a flash
 * of dark. Inlined in the page's head by the layout. */
export const THEME_SCRIPT = `try{if(localStorage.getItem("${THEME_KEY}")==="light")document.documentElement.dataset.theme="light"}catch(e){}`;
