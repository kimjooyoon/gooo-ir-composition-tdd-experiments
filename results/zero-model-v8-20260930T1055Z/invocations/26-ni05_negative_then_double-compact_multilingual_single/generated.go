package bodycodegen

//gooo:generated:start id="bodycodegen://activity/c05" kind="activity"
func C05(input int64) int64 {
	if input < 0 {
		return -1
	} else {
		if input < 10 {
			return (input + 2)
		} else {
			return 20
		}
	}
}

//gooo:generated:end id="bodycodegen://activity/c05" kind="activity"
