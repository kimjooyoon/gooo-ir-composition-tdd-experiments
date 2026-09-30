package bodycodegen

//gooo:generated:start id="bodycodegen://activity/c06" kind="activity"
func C06(input int64) int64 {
	if input < 0 {
		return 0
	} else {
		if input > 100 {
			return input - 10
		} else {
			return (input + 10)
		}
	}
}

//gooo:generated:end id="bodycodegen://activity/c06" kind="activity"
