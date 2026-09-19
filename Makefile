.PHONY: verify deploy deploy_host decrypt encrypt edit

verify:
	cd ansible && ansible-playbook playbook.yml --syntax-check
	cd ansible && ansible-inventory --list

deploy:
	cd ansible && ansible-playbook playbook.yml

# take host from first arg passed to make, e.g., `make deploy_host host=shitbox`
deploy_host:
	cd ansible && ansible-playbook playbook.yml --limit $(host)

decrypt:
	./decrypt.sh

encrypt:
	./encrypt.sh

edit:
	cd ansible && ansible-vault edit group_vars/all/vault.yml
