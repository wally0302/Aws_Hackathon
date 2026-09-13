// @lovable.dev/vite-tanstack-config already includes the following — do NOT add them manually
// or the app will break with duplicate plugins:
//   - TanStack devtools (dev-only, first), tanstackStart, viteReact, tailwindcss, tsConfigPaths,
//     nitro (build-only using cloudflare as a default target), VITE_* env injection, @ path alias,
//     React/TanStack dedupe, error logger plugins, and sandbox detection (port/host/strictPort).
// You can pass additional config via defineConfig({ vite: { ... }, etc... }) if needed.
import { defineConfig } from "@lovable.dev/vite-tanstack-config";

export default defineConfig({
  tanstackStart: {
    // Redirect TanStack Start's bundled server entry to src/server.ts (our SSR error wrapper).
    // nitro/vite builds from this
    server: { entry: "server" },

    // ⚠️⚠️ **SPA 模式：要部署到 S3 + CloudFront 就一定要開。**
    //
    // 預設是 SSR，`vite build` 產出的是 **Cloudflare Worker**
    // （`.output/server/wrangler.json`），`.output/public` 裡**連一個
    // index.html 都沒有**——靜態主機放不了。
    //
    // 開了之後會預先算出一份「殼」HTML，路由與取資料全部在瀏覽器跑。
    // 這個專案本來就不需要 SSR：
    //   - 沒有任何 `createServerFn` / server route（查過）
    //   - 沒有任何 route `loader`（查過）
    //   - 資料全部由 React Query 在瀏覽器向 API Gateway 要
    //
    // ⚠️ `outputPath` 預設是 `/_shell`，會產出 `.output/public/_shell/index.html`。
    //    改成 `/index.html` 才對得上 CloudFront 的 Default Root Object。
    //
    // ⚠️ **深層連結要靠 CloudFront 的錯誤頁轉址**：使用者直接開
    //    `/step4` 時 S3 上沒有那個物件，會回 403（用 OAC 時不是 404），
    //    要在 distribution 設 403/404 → `/index.html` 並回 200，
    //    不然重新整理任何一頁都會壞掉。
    spa: {
      enabled: true,
      prerender: {
        enabled: true,
        outputPath: "/index.html",
        // 只要殼，不要爬連結去預先渲染每一頁——這個站的每一頁都要
        // 先登入、而且內容全部來自 API，預先渲染沒有意義
        crawlLinks: false,
      },
    },
  },
});
