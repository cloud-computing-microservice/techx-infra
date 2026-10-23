terraform {
  backend "s3" {
    key                 = "staging/terraform.tfstate"
    region              = "us-east-1"
    encrypt             = true
    use_lockfile        = true
    allowed_account_ids = ["058114477594"]
  }
}
