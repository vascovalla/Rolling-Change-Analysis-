# Price overlay scaling

Approved user request: add Price, Rebase100, %Change and Z-score to the standalone app; default multiple overlays to Rebase100, preserve actual price in hover, rolling charts default percentage. Implement on current branch; test, code-review, commit. Base0314581.

Use existing public calculation exports and actual-file browser controls as test seams. Price overlays only; rolling-change calculations/summary and stored database observations remain unchanged. Auto default Price for one selected instrument and Rebase100 for2+ until user explicitly chooses a scale. Persist explicit choice; old settings without a choice retain automatic defaults.

Rebase/% use earliest common actual observation date within the displayed interval across instruments that have positive prices in that interval. Values before that date stay gaps. Instruments without display-range prices or a positive starting price stay gaps and do not prevent valid instruments from being compared. If eligible instruments have no common positive anchor, show an explanation rather than silently using different baselines. Rebase=100*price/base; %=100*(price/base-1).

Z-score=(price-trailingmean)/populationSD using60 actual closes, current included, no future observations. Fewer than60 closes or zero SD are gaps. Use loaded lookback history for warm-up; never use future display prices. Label60-close calculation. Raw/normalized/z hover shows actual native-unit price, plus plotted scaled value when different. Scale switch redraws locally without provider calls; preserve selected scale during polling and reload. Default rolling-change mode remains%, existing explicit user settings survive.

Validation: worked1000->1100 and10->11 both100->110; common-date/gap/zero cases; Z-score warm-up,constantseries,knownoutlier,no-lookahead; sourceimmutability. Browser defaults/modepersist/axes/tooltips/fetchcount/rollingindependence/mobile. FullPython+JSsuite once after implementation. Review standards andspec against0314581; no push withoutuserY.
