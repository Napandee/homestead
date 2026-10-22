/* The app form that Deploy and Edit share (#544): one list of sections in one
   order, and one block that lists the containers in each pod. Each view still
   draws its own fields; this file says where they go. */

/* Every section, in order. Deploy adds its Summary after these. */
const APP_FORM_SECTIONS = Object.freeze([
  { key: "basics", title: "Basics" },
  { key: "hardware", title: "Hardware and access" },
  { key: "environment", title: "Environment values" },
  { key: "storage", title: "Storage" },
  { key: "placement", title: "Placement" },
  { key: "address", title: "Address" },
  { key: "monitoring", title: "Monitoring" },
].map(section => Object.freeze(section)));

/* The sections that show each container's part of their topic, one panel per container. */
const APP_FORM_CONTAINER_SECTIONS = Object.freeze(["basics", "hardware", "environment", "storage", "address"]);

/* The sections for UI.sectionForm, from the HTML the view drew for each key.
   later: { key: "when it can be set" } greys a section the form cannot set yet. */
function appFormSections(mode, html, { leads = {}, later = {} } = {}) {
  const keys = [...APP_FORM_SECTIONS, ...(mode === "deploy" ? [{ key: "summary", title: "Summary" }] : [])];
  return keys.map(({ key, title }) => {
    if (typeof html[key] !== "string") throw new Error(`app form: no html for ${key}`);
    return { key, title, html: html[key], ...(leads[key] ? { lead: leads[key] } : {}), ...(later[key] ? { later: later[key] } : {}) };
  });
}

/* Basics' list of containers: the add button, how they share a pod, their
   panels and the ones removed on save. prefix is "e" (Edit) or "d" (Deploy). */
function appFormContainerBlock(prefix, mode, panelsHtml) {
  return `<div class="app-form-containers" id="${prefix}_containers_block">
    <div class="between"><div class="sec">Containers in each pod</div>
      <button class="btn" type="button" onclick="containerAdd(${jsq(mode)})">＋ Add container</button></div>
    ${UI.more("How containers share a pod", `<p>These containers share networking and volumes. ${mode === "edit" ? "Set pod copies under Placement." : "Set pod copies above."}</p>`)}
    <div id="${prefix}_basics_containers">${panelsHtml}</div>
    <div id="${prefix}_removed_containers" class="dim small"></div></div>`;
}

/* A container's panel in every section that holds one. */
function appFormAddPanels(prefix, container, index, mode) {
  for (const section of APP_FORM_CONTAINER_SECTIONS)
    document.getElementById(`${prefix}_${section}_containers`).insertAdjacentHTML("beforeend", editContainerPanel(container, index, section, mode));
}
