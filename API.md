# watchlink.co API (reverse-engineered)

Extracted from the public Vite bundle at `https://watchlink.co/assets/index-*.js`.
Everything below is *unofficial* — subject to change without notice.

## Base

```
https://api.watchlink.co/api/v1
```

Frontend preconnects to this host from `watchlink.co` (see `<link rel="preconnect">`
in the SPA shell). All requests use `credentials: "include"` + `mode: "cors"`.

## Auth

Rails-style session — HTTP-only session cookie on `api.watchlink.co` plus a
rotating CSRF token sent as `X-CSRF-Token` on every mutation.

### `POST /refresh`

Mints a fresh CSRF token. Call before any write.

- Headers: `Content-Type: application/json`, optionally `X-CSRF-Token: <old>`
- Body: *empty*
- Response:
  ```json
  { "access_token": "…", "csrf_token": "…" }
  ```
- `401` → session is dead, force user back to login.

Relevant bundle source:

```js
async function Nk({csrfToken:t}){
  const e = await fetch(`${l3}/refresh`, {
    method:"POST",
    headers:{...Ak, ...t?{"X-CSRF-Token":t}:{}},
    mode:"cors", credentials:"include"
  });
  // ...
  return { accessToken:n.access_token, csrfToken:n.csrf_token };
}
```

## Reads

### `GET /users/{userId}/posts`

Daniel's profile (UUID from the profile page path works directly).

Query params:
- `status`: `"published"` (also `"draft"` exists)
- `per_page`: up to 20 in the official client
- `page`: 1-indexed

### `GET /feed`

The global feed. The sniper uses this for buy/sell watch.

Query params:
- `category`: `"buy_sell"` (seen), other values exist
- `per_page`: 20
- `page`: 1-indexed
- `post_id`, `sort`, and optional filter block `{brand_include, brand_exclude, brand_all, condition, price_min, price_max}`.

### `GET /posts/{postId}/comments`

Thread for a single post. Paginated `per_page`/`page`.

## Writes

### `POST /posts/{postId}/comments`

The one the sniper uses.

- Headers: `Content-Type: application/json`, `X-CSRF-Token: <current>`
- Body:
  ```json
  { "comment": { "text": "me", "parent_comment_id": null } }
  ```
- `parent_comment_id` is non-null only for threaded replies.

Bundle source:

```js
.post({
  endpoint: `/posts/${t.toString()}/comments`,
  data: { comment: { text: e, parent_comment_id: n } }
})
```

### Other mutations (not used, documented for reference)

- `POST /posts/{id}/reactions` — add reaction
- `DELETE /posts/{id}/reactions/{emoji}` — remove reaction
- `POST /comments/{id}/reactions` / `DELETE /comments/{id}/reactions/{emoji}`
- `POST /polls/{id}/vote`

## Other interesting endpoints

| Endpoint | Purpose |
|---|---|
| `GET /feed/price_range` | Price filter bounds for the feed |
| `GET /users/{id}/post_photos` | Gallery |
| `GET /users/{id}/profile` | Profile meta |
| `GET /users/profiles/batch` | Bulk lookup |
| `GET /notifications`, `.../unread_count`, `.../{id}/mark_as_read` | Notifications |
| `GET /chat/token`, `GET /chat/mutes` | CometChat integration (non-API-v1 flow) |
| `POST /sessions` | Email/password login — body `{"session":{"login":"…","password":"…"}}`, returns `{access_token, csrf_token, profile}`, sets session cookie. 401 bad creds, 403 account-locked / terms issue. |
| `DELETE /sessions` | Logout |
| `POST /signup` | Signup |
| `POST /forgot-password`, `POST /reset-password` | Password reset |
| `GET /brands`, `GET /search/`, `GET /uploads/presign` | Misc |

## Error shape

- `401` on any endpoint → session expired. Calling `/refresh` after 401
  sometimes recovers it (the SPA retries once); otherwise require re-login.
- `4xx` error bodies use standard JSON — the SPA reads `.errors` for validation
  issues.

## Rate limiting

Not observed / not documented. The sniper hits two reads + at most ~10 writes
per 5-minute run — comfortably below anything a normal human would do.
