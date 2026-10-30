// Vite's `?url` suffix returns the emitted asset's URL, which TypeScript does not
// know about on its own.
declare module "*?url" {
  const url: string;
  export default url;
}
