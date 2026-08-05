# iNaturalist API v2

Concise reference for the iNaturalist Node API, generated from the published
[OpenAPI 3.0 schema](https://api.inaturalist.org/v2/api-docs) and the
[interactive documentation](https://api.inaturalist.org/v2/docs/).

- **Base URL:** `https://api.inaturalist.org/v2`
- **Schema version:** `2.2.0`
- **Formats:** JSON/JSONP; PNG for map tiles
- **Authentication:** JWT for authenticated requests

The OpenAPI document is authoritative for request-body properties, response
schemas, and changes to this API.

## Quick start

```bash
curl 'https://api.inaturalist.org/v2/observations?taxon_id=47126&per_page=10&fields=id,uuid,observed_on'
```

Typical collection responses contain `results`, `total_results`, `page`, and
`per_page`. Single-resource and action responses may return an object directly.
Errors use an `Error` object with `status` and an `errors` array containing
`errorCode` and `message`.

## Authentication and request rules

Obtain a JWT through an OAuth-authenticated request to
`https://www.inaturalist.org/users/api_token`; tokens expire after 24 hours.
Send the bare JWT in the API-key-style Authorization header:

```http
Authorization: <jwt>
```

All `POST` and `PUT` operations require authentication. Some `GET` operations
return private data, such as hidden coordinates, only when the authenticated
user is allowed to see it. `DELETE` operations that alter account content also
require authentication and appropriate ownership or moderation permissions.

For JSON writes, send `Content-Type: application/json` and a JSON body. Upload
operations may use `multipart/form-data` where indicated by the schema.

### Response field selection

Responses are intentionally minimal by default. Use `fields` to request the
properties needed by the client:

```text
GET /observations?fields=id,uuid,species_guess,observed_on
GET /observations?fields=(species_guess:!t,user:(login:!t))
GET /observations?fields=all
```

The preferred nested-field syntax is [RISON](https://github.com/w33ble/rison-node).
For long field expressions, send a `POST` with
`X-HTTP-Method-Override: GET`, `Content-Type: application/json`, and put the
fields object in the JSON body. Multipart requests may carry `fields` as a
comma-separated list or JSON string.

### Pagination and lists

Most list endpoints accept `page` and `per_page`; use the endpoint schema for
limits and defaults. Multiple values for one URL parameter are comma-separated:
`taxon_id=47126,47158`. Dates are generally ISO dates or date-times, depending
on the endpoint.

## Endpoint reference

All paths below are relative to the base URL. `{id}`, `{uuid}`, `{taxon_id}`,
`{metric}`, `{zoom}`, `{x}`, and `{y}` are path parameters.

### Health, search, users, and localization

| Method | Path | Purpose |
|---|---|---|
| GET | `/ping` | Check API availability |
| GET | `/build_info` | API build information |
| GET | `/app_build_info` | Application/API/vision build information |
| GET | `/search` | Search across multiple record types; supports `q`, `locale`, `place_id`, `preferred_place_id`, `sources`, `include_taxon_ancestors`, `page`, `per_page`, `fields` |
| GET | `/sites` | List sites |
| GET | `/translations/locales` | List translated locales |
| GET | `/users` | List users; supports `following`, `followed_by`, `orcid`, pagination |
| GET | `/users/autocomplete` | Search users with `q` |
| GET | `/users/{id}` | Fetch a user |
| PUT | `/users/{id}` | Update a user |
| GET | `/users/{id}/projects` | Projects joined or followed by a user |
| POST/DELETE | `/users/{id}/mute` | Add/remove a mute |
| POST/DELETE | `/users/{id}/block` | Add/remove a block |
| GET | `/users/me` | Fetch the authenticated user |
| PUT | `/users/update_session` | Update the authenticated session; supports `prefers_hide_obs_show_identifiers` |
| GET | `/users/recent_observation_fields` | Recently used observation fields for the authenticated user |
| GET | `/users/notification_counts` | Unread notification counts |
| GET | `/users/email_available` | Check whether an email can be used; `email` is required |
| POST | `/users/reset_password` | Reset a password |
| POST | `/users/resend_confirmation` | Resend an email confirmation |

### Taxa and controlled vocabulary

| Method | Path | Purpose |
|---|---|---|
| GET | `/taxa` | Search taxa by name, ID, parent, rank, rank level, active state, iconic group, locale, or preferred place |
| GET | `/taxa/autocomplete` | Autocomplete taxon names with `q` |
| GET | `/taxa/iconic` | Fetch iconic taxa |
| GET | `/taxa/{id}` | Fetch a taxon |
| GET | `/taxa/{id}/wanted` | Fetch unobserved taxa in a clade |
| GET | `/taxa/suggest` | Suggest taxa from observation conditions; supports location, dates, months, place, image URL, and source |
| POST | `/taxa/suggest` | Suggest taxa using an image/request body |
| GET | `/controlled_terms` | Search controlled terms |
| GET | `/controlled_terms/for_taxon/{taxon_id}` | Fetch terms valid for a taxon |
| POST | `/taxon_name_priorities` | Create a taxon-name priority |
| PUT/DELETE | `/taxon_name_priorities/{id}` | Update/delete a taxon-name priority |

Common taxon parameters include `q`, `taxon_id`, `parent_id`, `rank`,
`rank_level`, `id_above`, `id_below`, `is_active`, `iconic`, `locale`,
`preferred_place_id`, `per_page`, and `fields`.

### Observations

#### Core resources and actions

| Method | Path | Purpose |
|---|---|---|
| GET | `/observations` | Search observations |
| POST | `/observations` | Create one or more observations |
| GET | `/observations/{uuid}` | Fetch an observation |
| PUT | `/observations/{uuid}` | Update an observation |
| DELETE | `/observations/{uuid}` | Delete an observation |
| GET | `/observations/deleted` | IDs deleted by the authenticated user |
| PUT | `/observations/{uuid}/viewed_updates` | Mark observation updates viewed |
| GET | `/observations/{uuid}/taxon_summary` | Taxon summary; optional `community` |
| GET | `/observations/{uuid}/subscriptions` | Authenticated user's subscriptions |
| PUT | `/observations/{uuid}/subscription` | Toggle subscription |
| POST/DELETE | `/observations/{uuid}/review` | Add/remove a review |
| POST/DELETE | `/observations/{uuid}/fave` | Add/remove a favorite |
| GET | `/observations/{uuid}/quality_metrics` | Fetch quality metrics |
| POST/DELETE | `/observations/{uuid}/quality/{metric}` | Vote/remove vote on a quality metric; `agree` is used when voting |

#### Observation search filters

The same filter family is reused by observation statistics, nearby-place, and
map-tile endpoints. Important filters include:

| Group | Parameters |
|---|---|
| Taxon | `taxon_id`, `without_taxon_id`, `taxon_name`, `iconic_taxa`, `rank`, `hrank`, `lrank`, `id_above`, `id_below`, `taxon_is_active` |
| People/projects | `user_id`, `user_login`, `project_id`, `list_id`, `not_in_project`, `apply_project_rules_for`, `viewer_id` |
| Place/geometry | `place_id`, `lat`, `lng`, `radius`, `nelat`, `nelng`, `swlat`, `swlng`, `geo`, `coords_viewable_for_proj` |
| Date/time | `observed_on`, `created_on`, `d1`, `d2`, `created_d1`, `created_d2`, `year`, `month`, `day`, `hour`, `created_year`, `created_month`, `created_day`, `date_field`, `interval` |
| Evidence | `photos`, `sounds`, `licensed`, `photo_licensed`, `license`, `photo_license`, `sound_license`, `outlink_source` |
| Quality/status | `quality_grade`, `verifiable`, `reviewed`, `identifications`, `disagreements`, `fails_dqa_accurate`, `fails_dqa_date`, `fails_dqa_evidence`, `fails_dqa_location`, `fails_dqa_needs_id`, `fails_dqa_recent`, `fails_dqa_subject`, `fails_dqa_wild`, `not_casual_excluding_captive` |
| Annotation | `term_id`, `term_value_id`, `without_term_value_id`, `term_id_or_unknown`, `annotation_user_id`, `without_field` |
| Privacy/accuracy | `acc`, `acc_above`, `acc_below`, `acc_below_or_unknown`, `geoprivacy`, `taxon_geoprivacy`, `obscuration`, `captive`, `native`, `introduced`, `endemic`, `threatened`, `out_of_range`, `mappable` |
| Result control | `q`, `search_on`, `updated_since`, `expected_nearby`, `page`, `per_page`, `order`, `order_by`, `only_id`, `locale`, `preferred_place_id`, `ttl`, `fields` |

#### Observation aggregations and feeds

| Method | Path | Purpose |
|---|---|---|
| GET | `/observations/updates` | Observation updates; `created_after`, `viewed`, `observations_by` |
| GET | `/observations/species_counts` | Species counts for the observation filter set; optional `include_ancestors` |
| GET | `/observations/quality_grades` | Quality-grade counts |
| GET | `/observations/popular_field_values` | Popular controlled-term values and monthly histogram; supports `no_histograms`, `unannotated` |
| GET | `/observations/observers` | Observer counts |
| GET | `/observations/identifiers` | Identifier counts |
| GET | `/observations/identification_categories` | Identification-category counts |
| GET | `/observations/iconic_taxa_species_counts` | Species counts by iconic taxon |
| GET | `/observations/histogram` | Observation histogram by date and interval |
| GET | `/observations/umbrella_project_stats` | Statistics for umbrella projects |

### Identifications, annotations, comments, and flags

| Method | Path | Purpose |
|---|---|---|
| GET | `/identifications/similar_species` | Similar species |
| GET | `/identifications/recent_taxa` | Recent identifications and taxa |
| GET | `/identifications/identifiers` | Most active identifiers |
| POST | `/identifications` | Create an identification |
| PUT/DELETE | `/identifications/{uuid}` | Update/delete an identification |
| POST | `/annotations` | Create an annotation |
| POST/DELETE | `/annotations/{uuid}/vote` | Vote/remove vote on an annotation |
| DELETE | `/annotations/{uuid}` | Delete an annotation |
| POST | `/comments` | Create a comment |
| PUT/DELETE | `/comments/{uuid}` | Update/delete a comment |
| POST | `/flags` | Create a flag |
| PUT/DELETE | `/flags/{id}` | Update/delete a flag |

### Computer vision and exemplar identifications

| Method | Path | Purpose |
|---|---|---|
| POST | `/computervision/score_image` | Get computer-vision suggestions for an image |
| GET | `/computervision/language_search` | Get language-demo results for a search term |
| GET | `/computervision/score_observation/{uuid}` | Get computer-vision suggestions for an observation |
| GET | `/exemplar_identifications` | Search exemplar identifications |
| POST/DELETE | `/exemplar_identifications/{id}/vote` | Vote/remove vote on an exemplar identification |

### Photos, sounds, and observation attachments

| Method | Path | Purpose |
|---|---|---|
| POST | `/photos` | Create/upload a photo |
| PUT | `/photos/{id}` | Update a photo |
| POST | `/sounds` | Create/upload a sound |
| POST | `/observation_photos` | Attach a photo to an observation |
| PUT/DELETE | `/observation_photos/{uuid}` | Update/delete an observation photo attachment |
| POST | `/observation_sounds` | Attach a sound to an observation |
| PUT/DELETE | `/observation_sounds/{uuid}` | Update/delete an observation sound attachment |
| POST | `/observation_field_values` | Add an observation-field value |
| PUT/DELETE | `/observation_field_values/{uuid}` | Update/delete an observation-field value |

Photo URLs support `original`, `large`, `medium`, `small`, `thumb`, and
`square` variants (maximum dimensions approximately 2048, 1024, 500, 240,
100, and 75px respectively). Replace only the size qualifier in a URL. The
hosting domain reflects the photo license; do not assume every static-hosted
photo is openly licensed.

### Projects and memberships

| Method | Path | Purpose |
|---|---|---|
| GET | `/projects` | Search projects by name, place, location, type, site, member, features, rules, or posts |
| GET | `/projects/{id}` | Fetch a project |
| GET | `/projects/{id}/posts` | Fetch project posts |
| GET | `/projects/{id}/members` | Fetch project members |
| GET | `/projects/{id}/membership` | Current user's membership |
| POST/DELETE | `/projects/{id}/membership` | Join/leave a project |
| PUT | `/projects/{id}/feature` | Feature a project |
| PUT | `/projects/{id}/unfeature` | Remove a project from featured |
| POST | `/project_observations` | Add an observation to a project |
| PUT/DELETE | `/project_observations/{uuid}` | Update/remove a project observation |
| PUT | `/project_users/{id}` | Update project-user membership |

### Places and maps

| Method | Path | Purpose |
|---|---|---|
| GET | `/places` | Search places; supports `q`, `order_by`, `geo`, `per_page`, `fields` |
| GET | `/places/nearby` | Find nearby places using observation-style filters |
| GET | `/places/{uuid}` | Fetch a place |
| GET | `/places/{id}/{zoom}/{x}/{y}.png` | Place tile |
| GET | `/points/{zoom}/{x}/{y}.png` | Point observation tile |
| GET | `/points/{zoom}/{x}/{y}.grid.json` | UTFGrid JSON for point tile |
| GET | `/grid/{zoom}/{x}/{y}.png` | Grid tile |
| GET | `/grid/{zoom}/{x}/{y}.grid.json` | UTFGrid JSON for grid tile |
| GET | `/heatmap/{zoom}/{x}/{y}.png` | Heatmap tile |
| GET | `/taxon_ranges/{id}/{zoom}/{x}/{y}.png` | Taxon range tile |
| GET | `/taxon_places/{id}/{zoom}/{x}/{y}.png` | Taxon-place tile |
| GET | `/geomodel/{id}/{zoom}/{x}/{y}.png` | Taxon geomodel tile; optional `thresholded` |
| GET | `/geomodel_taxon_range/{id}/{zoom}/{x}/{y}.png` | Geomodel taxon-range tile |
| GET | `/geomodel_comparison/{id}/{zoom}/{x}/{y}.png` | Geomodel comparison tile |

Tiles use XYZ addressing. Observation tile endpoints accept nearly all
observation-search filters. `tile_size` is available on tile endpoints.

### Messages, posts, relationships, and saved locations

| Method | Path | Purpose |
|---|---|---|
| GET | `/messages` | Retrieve authenticated user's messages |
| POST | `/messages` | Create a message |
| GET | `/messages/{id}` | Retrieve a message thread |
| GET | `/posts/for_user` | Fetch posts; supports `newer_than`, `older_than`, pagination |
| GET | `/relationships` | Search authenticated user's relationships |
| POST | `/relationships` | Create a relationship |
| PUT/DELETE | `/relationships/{id}` | Update/delete a relationship |
| GET | `/saved_locations` | Retrieve authenticated user's saved locations |
| POST | `/saved_locations` | Create a saved location |
| DELETE | `/saved_locations/{id}` | Delete a saved location |

### Account authorizations and announcements

| Method | Path | Purpose |
|---|---|---|
| GET | `/authorized_applications` | Applications authorized for the authenticated user |
| DELETE | `/authorized_applications/{id}` | Revoke application authorization |
| GET | `/provider_authorizations` | Third-party accounts authorized through iNat |
| DELETE | `/provider_authorizations/{id}` | Revoke a provider authorization |
| GET | `/announcements` | Search announcements |
| PUT | `/announcements/{id}/dismiss` | Dismiss an announcement |
| POST | `/log` | Log client-application events |

## Core response models

The schema defines many specialized `Results*` wrappers. The following are
the principal resource models and their commonly exposed fields; nullable and
permission-dependent properties may be omitted.

| Model | Key fields / relationships |
|---|---|
| `Observation` | `id`, `uuid`, dates, location/geoprivacy, `taxon`, `user`, `photos`, `sounds`, `identifications`, annotations, quality grade, project memberships |
| `Taxon` | `id`, `uuid`, `name`, rank, ancestry, parent/ancestors/children, common names, photos, conservation status, endemic/native/introduced/threatened flags |
| `User` | `id`, `uuid`, login/name, icon, counts, roles, site, preferences, timestamps |
| `Photo` | `id`, URLs, attribution, license, dimensions, native-page metadata, moderation flags |
| `Sound` | Audio URL, attribution/license and metadata for an observation sound |
| `Place` | `id`, `uuid`, name, display name, place type, ancestry, point and polygon GeoJSON, observation count |
| `Identification` | Taxon, observer/identifier, observation ID, category, current/disagreement state, timestamps, votes and moderation data |
| `Annotation` | Controlled attribute/value, user, UUID, vote score and votes |
| `Comment` | UUID, body/HTML, user, parent, timestamps, flags, moderation state |
| `Project` | Project identity, title/type, rules/features, site/place, members and posts |
| `ControlledTerm` | Attribute/value IDs, labels, ontology URI, taxon constraints, nested values |
| `QualityMetric` | Metric identity, agreement state, votes, and observation-quality metadata |
| `Error` | `status`; `errors[]` with `errorCode`, `message`, optional source and stack |

GeoJSON coordinates follow the usual `[longitude, latitude]` order. Point and
polygon models are available as `PointGeoJson` and `PolygonGeoJson`.

## Operational guidance

- Keep traffic at or below 60 requests/minute when possible. The documented
  hard throttle is 100 requests/minute; stay under 10,000 requests/day.
- Cache stable GET responses and request only required fields.
- Use paginated requests and avoid repeatedly downloading full observation
  records or all fields.
- Treat private coordinates and other permission-dependent fields as sensitive.
- API use is subject to the [Terms of Service](https://www.inaturalist.org/terms)
  and [Privacy Policy](https://www.inaturalist.org/privacy). The API is intended
  for application development, not unrestricted scraping; use pre-generated
  [data exports](https://www.inaturalist.org/pages/developers) for bulk data.

## Official references

- [Interactive API documentation](https://api.inaturalist.org/v2/docs/)
- [OpenAPI JSON schema](https://api.inaturalist.org/v2/api-docs)
- [Developer resources](https://www.inaturalist.org/pages/developers)
- [iNaturalist AWS Open Data](https://registry.opendata.aws/inaturalist-open-data/)
