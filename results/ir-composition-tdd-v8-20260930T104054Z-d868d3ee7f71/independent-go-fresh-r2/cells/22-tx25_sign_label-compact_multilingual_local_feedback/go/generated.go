package bodycodegen

//gooo:generated:start id="bodycodegen://activity/c25" kind="activity"
func C25(input int64) int64 {
	var label = "neutral"
	if input < 0 {
		label = "negative"
	} else {
		label = "nonnegative"
	}
	if label == "negative" {
		return -1
	} else {
		return 1
	}
}

//gooo:generated:end id="bodycodegen://activity/c25" kind="activity"
