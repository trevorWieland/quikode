# Identity Policy

The identity-policy subsystem authenticates principals and enforces
permission semantics across the API surface.

## Permissions

Permissions are scope-tagged and immutable per request. The runtime
checks every cross-subsystem call.

## Error Taxonomy

`AuthError::Unauthenticated` precedes `AuthError::Forbidden` in the
chain; clients differentiate via the typed variant.
