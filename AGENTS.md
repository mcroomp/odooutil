# Mandatory pre-commit privacy review

Before every commit or other check-in, manually review the complete staged diff
and every newly staged file for private information from any real Odoo instance.
This review is mandatory even when tests, linters and automated secret scans pass.

- Never commit credentials, API keys, tokens, cookies, connection files, real
  tenant URLs or database names, or other deployment-specific information.
- Never commit real records, personal or business data, messages, attachments,
  financial details, or identifiers that can be linked back to a real instance.
- Never commit live exports, database dumps, snapshots, RPC transcripts, private
  schema/spec exports, recovery journals, or logs containing real instance data.
- Review code, tests, fixtures, documentation, examples, generated files and the
  proposed commit message. Renaming, truncating, hashing or masking real values
  does not make the surrounding data safe to publish.
- Use wholly synthetic examples and mocked transports. Do not authenticate to or
  query a live Odoo instance to generate or validate committed test data.
  Published standard Odoo field definitions are acceptable; private custom
  schemas and organization-specific metadata are not.
- Stage explicit, reviewed paths. Do not bulk-stage untracked local directories.
  Keep credential files and private local artifacts out of both Git and review
  output; do not print their contents while checking for accidental inclusion.

If provenance is uncertain or any private information is found, stop the check-in.
Remove it from the proposed changes, replace necessary examples with synthetic
data, and repeat the complete staged privacy review before committing. Never
waive this requirement to finish a task or open a pull request.
