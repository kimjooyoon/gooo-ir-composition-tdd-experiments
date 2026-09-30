package bodycodegen

//gooo:generated:start id="bodycodegen://activity/c27" kind="activity"
func C27(input int64) int64 {
	var label = "plum"
	if input < 0 {
		label = "amber"
	} else {
		label = "zinc"
	}
	if label < "amber" {
		return 1
	} else {
		return 0
	}
}

//gooo:generated:end id="bodycodegen://activity/c27" kind="activity"
