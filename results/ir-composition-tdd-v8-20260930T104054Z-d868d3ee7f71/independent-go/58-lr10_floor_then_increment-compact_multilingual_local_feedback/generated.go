package bodycodegen

//gooo:generated:start id="bodycodegen://activity/c10" kind="activity"
func C10(input int64) int64 {
	var adjusted = input
	if adjusted < 0 {
		adjusted = 0
	} else {
		adjusted = adjusted + 1
	}
	return adjusted
}

//gooo:generated:end id="bodycodegen://activity/c10" kind="activity"
