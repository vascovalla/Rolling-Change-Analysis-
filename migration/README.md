# Risk source removal

Base: `0e2d41be8723c416e9bf140e889608e30d83fc92` (personal repository v1168).
Prepared source commit: `f242d1b122647306773b8a148631e77d8b5149ee` (v1169).

This patch removes the Rolling Analysis tab, its assets, setting key and dedicated history service/routes/tests. Other rolling charts and existing price caches/databases are retained.

Apply on a clean compatible Risk checkout using `git am remove-from-risk.patch`, review and test before publishing to its approved repository. Do not apply blindly to a different organization release or overwrite unique changes. This extraction does not change the live N1 service.

Validation: Risk server syntax, all four inline script blocks, 14 strike release checks and Positions combobox/render-race checks passed.
