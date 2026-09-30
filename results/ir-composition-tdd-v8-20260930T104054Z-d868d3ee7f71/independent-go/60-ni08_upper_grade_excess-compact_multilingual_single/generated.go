package bodycodegen

//gooo:generated:start id="bodycodegen://activity/c08" kind="activity"
func C08(input int64) int64 {
	if input < 60 {
		return -1
	} else {
		if input < 80 {
			return 0
		} else {
			return (input - 81)
		}
	}
}

//gooo:generated:end id="bodycodegen://activity/c08" kind="activity"
