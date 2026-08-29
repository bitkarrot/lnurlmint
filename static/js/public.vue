<template id="page-lnurlmint-public">
  <div class="row justify-center q-col-gutter-md">
    <div class="col-12 col-md-8">
      <!-- Loading state -->
      <q-card v-if="loading" class="q-pa-lg text-center">
        <q-spinner size="40px" color="primary" />
        <div class="text-grey q-mt-sm">Loading mint info…</div>
      </q-card>

      <!-- Not found -->
      <q-card v-else-if="notFound" class="q-pa-lg text-center">
        <q-icon name="error" size="40px" color="negative" />
        <div class="text-h6 q-mt-sm">Mint not found</div>
        <div class="text-grey">This mint does not exist or has been removed.</div>
      </q-card>

      <!-- Main content -->
      <q-card v-else-if="mint">
        <q-card-section>
          <div class="text-h5 q-mb-sm">{{ mint.username }}</div>

          <!-- Sunset notice -->
          <q-banner
            v-if="mint.sunset_mint"
            class="bg-orange text-white q-mb-md"
          >
            <q-icon name="warning" class="q-mr-sm" />
            This mint is no longer issuing notes (sunset mode).
          </q-banner>

          <!-- QR code section (hidden when sunsetting) -->
          <div v-if="!mint.sunset_mint" class="text-center q-mb-md">
            <lnbits-qrcode
              :value="mint.lnurl"
              :options="{ width: 240 }"
              class="q-mb-sm"
            />
            <div class="row justify-center q-gutter-sm">
              <q-btn
                flat
                dense
                color="primary"
                :icon="copied ? 'check' : 'content_copy'"
                :label="copied ? 'Copied!' : 'Copy LNURL'"
                @click="copyLnurl"
              />
            </div>
            <q-input
              filled
              readonly
              v-model="mint.lnurl"
              type="textarea"
              class="q-mt-sm"
              input-class="text-caption"
            />
          </div>

          <!-- Mint limits -->
          <div class="text-subtitle2 q-mb-xs">Limits</div>
          <q-list dense bordered separator class="q-mb-md">
            <q-item>
              <q-item-section>Minimum mint</q-item-section>
              <q-item-section side>{{ formatSats(mint.min_mint_msat) }}</q-item-section>
            </q-item>
            <q-item>
              <q-item-section>Maximum mint (fee-aware)</q-item-section>
              <q-item-section side>{{ formatSats(mint.max_mintable_msat) }}</q-item-section>
            </q-item>
          </q-list>

          <!-- Mint pubkey -->
          <div class="text-subtitle2 q-mb-xs">Mint Public Key</div>
          <q-input
            filled
            readonly
            v-model="mint.mint_pubkey"
            type="textarea"
            class="q-mb-md"
            input-class="text-caption"
          />

          <!-- Node info -->
          <div class="text-subtitle2 q-mb-xs">Funding Node</div>
          <div v-if="mint.node_info" class="q-mb-md">
            <q-list dense bordered separator>
              <q-item>
                <q-item-section>
                  <q-item-label>{{ mint.node_info.alias }}</q-item-label>
                  <q-item-label caption>
                    <span
                      class="inline-block q-mr-xs"
                      :style="{
                        display: 'inline-block',
                        width: '12px',
                        height: '12px',
                        background: '#' + mint.node_info.color,
                        borderRadius: '2px',
                        verticalAlign: 'middle'
                      }"
                    ></span>
                    #{{ mint.node_info.color }}
                  </q-item-label>
                </q-item-section>
              </q-item>
              <q-item>
                <q-item-section>
                  <q-item-label caption>Pubkey</q-item-label>
                  <q-item-label class="text-caption">{{ mint.node_info.id }}</q-item-label>
                </q-item-section>
                <q-item-section side>
                  <div class="row q-gutter-xs">
                    <q-btn
                      flat
                      dense
                      color="primary"
                      label="mempool"
                      :href="'https://mempool.space/lightning/node/' + mint.node_info.id"
                      target="_blank"
                      type="a"
                    />
                    <q-btn
                      flat
                      dense
                      color="primary"
                      label="amboss"
                      :href="'https://amboss.space/node/' + mint.node_info.id"
                      target="_blank"
                      type="a"
                    />
                  </div>
                </q-item-section>
              </q-item>
              <q-item>
                <q-item-section>Capacity</q-item-section>
                <q-item-section side>{{ formatSats(mint.node_info.capacity_msat) }}</q-item-section>
              </q-item>
              <q-item>
                <q-item-section>Channels</q-item-section>
                <q-item-section side>{{ mint.node_info.num_channels }}</q-item-section>
              </q-item>
              <q-item>
                <q-item-section>Peers</q-item-section>
                <q-item-section side>{{ mint.node_info.num_peers }}</q-item-section>
              </q-item>
            </q-list>
          </div>
          <div v-else class="text-grey q-mb-md">
            Node info unavailable.
          </div>

          <!-- Tor section -->
          <div v-if="showTorSection" class="text-subtitle2 q-mb-xs">
            Tor Address
          </div>
          <q-banner
            v-if="showTorSection"
            class="bg-purple-1 q-mb-md"
          >
            <q-icon name="vpn_lock" class="q-mr-sm" color="purple" />
            <div class="row items-center">
              <div class="col">
                <div class="text-caption">{{ mint.onion_url }}</div>
                <div class="text-grey text-caption">
                  Scan over Tor for privacy.
                </div>
              </div>
              <div class="col-auto">
                <q-btn
                  flat
                  dense
                  color="purple"
                  icon="content_copy"
                  @click="copyOnion"
                />
              </div>
            </div>
          </q-banner>
        </q-card-section>
      </q-card>
    </div>
  </div>
</template>
